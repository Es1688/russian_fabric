"""Import reviewed content bank items from a YAML seed file (plan §6).

Operator tool for filling the bank - the runtime has no admin command for
this. Reads a seed file, validates every record, then inserts new items
through ContentBankRepository so serialization matches the runtime
exactly (months as a JSON array, metadata as JSON, UNIQUE(item_type,
text)). Re-runs are idempotent: a record that already exists with the
same (item_type, text) is skipped, never duplicated.

Seed format (UTF-8), every key optional except type/text:

    items:
      - type: phrase              # content_bank.item_type
        text: "Доброе утро! ..."   # post body (keep within rubric char limits)
        months: [3, 4, 5]         # seasonality 1..12; omitted = any month
        event_date: "05-09"       # 'MM-DD' for "this day" facts (type event)
        metadata:                 # postcards carry the image key
          image: morning_01.jpg   # relative to the postcards dir

Usage inside the container (the VPS data dir is mounted at /app/data):

    docker exec maxnewsbot python /app/max_news_bot/scripts/bank_import.py \
        --file /app/data/bank_seed.yaml

Items land as review_status='approved' - the operator reviews the seed
file itself before importing; pass --pending to stage items for a later
review instead. --dry-run validates and prints the plan without touching
the database (a missing database file is fine in dry-run: every record
counts as new). Postcard records with an image key require the file to
exist in the postcards dir (default <db dir>/postcards) so a typo cannot
produce a postcard the publisher would reject at 07:00.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import yaml

from max_news_bot.core.models import ContentBankItem
from max_news_bot.utils.db import Database
from max_news_bot.utils.repositories import ContentBankRepository

# mirrors bot/rubrics_runtime.py POSTCARDS_DIR_NAME (kept local so the
# tool does not pull the whole runtime wiring)
POSTCARDS_DIR_NAME = "postcards"

_TYPE_RE = re.compile(r"^[a-z0-9_]+$")
_ALLOWED_KEYS = {"type", "text", "months", "event_date", "metadata"}
# the daily summary alerts when a type's stock drops below 30 days
_STOCK_WARN = 30


def default_db_path() -> Path:
    """DATABASE_PATH env first, then the repo default (max_news_bot/data)."""
    env = os.getenv("DATABASE_PATH", "").strip()
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent / "data" / "max_news_bot.db"


def load_seed(path: Path) -> list[dict]:
    """Parse the seed file and return its raw items list."""
    with path.open(encoding="utf-8") as handle:
        document = yaml.safe_load(handle)
    if not isinstance(document, dict) or not isinstance(
        document.get("items"), list
    ):
        raise ValueError("seed file must be a mapping with an 'items' list")
    return document["items"]


def build_item(raw: object, index: int) -> ContentBankItem:
    """Validate one raw record into a bank item (image files not checked).

    Raises ValueError with the record's position on any problem so a typo
    in the seed cannot enter the bank.
    """
    where = f"items[{index}]"
    if not isinstance(raw, dict):
        raise ValueError(f"{where}: must be a mapping")
    unknown = set(raw) - _ALLOWED_KEYS
    if unknown:
        raise ValueError(f"{where}: unknown keys {sorted(unknown)}")

    item_type = raw.get("type")
    if not isinstance(item_type, str) or not _TYPE_RE.match(item_type):
        raise ValueError(f"{where}: 'type' must match [a-z0-9_]+")
    text = raw.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ValueError(f"{where}: 'text' must be a non-empty string")

    months = raw.get("months", [])
    if not isinstance(months, list) or any(
        not isinstance(month, int) or not 1 <= month <= 12 for month in months
    ):
        raise ValueError(f"{where}: 'months' must be a list of ints 1..12")
    if len(set(months)) != len(months):
        raise ValueError(f"{where}: 'months' has duplicates")

    event_date = raw.get("event_date")
    if event_date is not None:
        if not isinstance(event_date, str):
            raise ValueError(f"{where}: 'event_date' must be 'MM-DD'")
        try:
            datetime.strptime(event_date, "%m-%d")
        except ValueError:
            raise ValueError(
                f"{where}: 'event_date' {event_date!r} is not a valid MM-DD"
            ) from None

    metadata = raw.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise ValueError(f"{where}: 'metadata' must be a mapping")
    image = metadata.get("image")
    if image is not None and (
        not isinstance(image, str)
        or not image.strip()
        or ".." in image
        or "/" in image
        or "\\" in image
    ):
        raise ValueError(
            f"{where}: metadata.image must be a file name relative to "
            "the postcards dir"
        )

    return ContentBankItem(
        id=None,
        item_type=item_type,
        text=text,
        metadata=metadata,
        months=tuple(months),
        event_date=event_date,
    )


def check_images(
    items: list[ContentBankItem], postcards_dir: Path, strict: bool
) -> list[str]:
    """Verify every referenced postcard file exists; errors (or warnings)."""
    errors: list[str] = []
    referenced = [
        (index, item.metadata["image"])
        for index, item in enumerate(items)
        if item.metadata.get("image")
    ]
    if not referenced:
        return errors
    if not postcards_dir.is_dir():
        return [f"postcards dir not found: {postcards_dir}"] if strict else []
    for index, image in referenced:
        if not (postcards_dir / image).is_file():
            errors.append(
                f"items[{index}]: image {image!r} not found in {postcards_dir}"
            )
    return errors


def describe(item: ContentBankItem) -> str:
    label = item.item_type
    if item.event_date:
        label += f" {item.event_date}"
    preview = item.text[:50].strip()
    return f"{label}: {preview}{'...' if len(item.text) > 50 else ''}"


def existing_pairs(
    db_path: Path, items: list[ContentBankItem]
) -> set[tuple[str, str]]:
    """(item_type, text) pairs already in the bank; read-only probe."""
    pairs: set[tuple[str, str]] = set()
    if not db_path.is_file():
        return pairs
    with sqlite3.connect(db_path) as conn:
        for item in items:
            row = conn.execute(
                "SELECT 1 FROM content_bank WHERE item_type = ? AND text = ?",
                (item.item_type, item.text),
            ).fetchone()
            if row is not None:
                pairs.add((item.item_type, item.text))
    return pairs


async def run(args: argparse.Namespace) -> int:
    try:
        raw_items = load_seed(Path(args.file))
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"ERROR: cannot read seed file: {exc}", file=sys.stderr)
        return 1

    items: list[ContentBankItem] = []
    errors: list[str] = []
    for index, raw in enumerate(raw_items):
        try:
            items.append(build_item(raw, index))
        except ValueError as exc:
            errors.append(str(exc))

    db_path = Path(args.db) if args.db else default_db_path()
    postcards_dir = (
        Path(args.postcards_dir)
        if args.postcards_dir
        else db_path.parent / POSTCARDS_DIR_NAME
    )
    errors.extend(check_images(items, postcards_dir, strict=not args.dry_run))
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1

    present = existing_pairs(db_path, items)
    status = "pending" if args.pending else "approved"
    repo: ContentBankRepository | None = None
    if not args.dry_run:
        database = Database(db_path)
        await database.initialize()
        repo = ContentBankRepository(db_path)

    inserted = skipped = 0
    for item in items:
        label = describe(item)
        if (item.item_type, item.text) in present:
            skipped += 1
            print(f"skip (exists): {label}")
            continue
        if args.dry_run:
            inserted += 1
            print(f"would insert [{status}] {label}")
            continue
        assert repo is not None
        await repo.add(
            ContentBankItem(
                id=None,
                item_type=item.item_type,
                text=item.text,
                metadata=item.metadata,
                review_status=status,
                months=item.months,
                event_date=item.event_date,
            )
        )
        inserted += 1
        print(f"inserted [{status}] {label}")

    if repo is not None:
        stock = await repo.count_available_by_type()
        seed_types = {item.item_type for item in items}
        print("\nApproved stock by type:")
        for item_type in sorted(seed_types | set(stock)):
            count = stock.get(item_type, 0)
            mark = f"  (<{_STOCK_WARN}, LOW)" if count < _STOCK_WARN else ""
            print(f"  {item_type}: {count}{mark}")
    print(f"\n{inserted} inserted, {skipped} skipped, {len(items)} total")
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Import content bank items from a YAML seed file."
    )
    parser.add_argument("--file", required=True, help="path to the seed YAML")
    parser.add_argument(
        "--db",
        help="database path (default: DATABASE_PATH env or the repo data dir)",
    )
    parser.add_argument(
        "--postcards-dir",
        help="postcards dir (default: <db dir>/postcards)",
    )
    parser.add_argument(
        "--pending",
        action="store_true",
        help="insert as review_status=pending instead of approved",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate and print the plan without writing",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(run(parse_args(argv if argv is not None else sys.argv[1:])))


if __name__ == "__main__":
    raise SystemExit(main())
