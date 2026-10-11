"""Repositories for the autonomous-channel tables (plan §6, §9).

SQL lives in the storage layer only: rubric pipelines touch the database
exclusively through these classes. The connection and datetime helpers are
package-internal siblings of utils.db on purpose — one storage layer, one
set of conventions (WAL, busy timeout, UTC timestamps). SlotRun.slot_date
is a calendar date computed by the scheduler in the config timezone and is
treated here as an opaque 'YYYY-MM-DD' string.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from max_news_bot.core.models import (
    ContentBankItem,
    DigestItemRef,
    FilterLogEntry,
    NewsItem,
    RubricState,
    SlotRun,
)
from max_news_bot.utils.db import (
    _MAX_ERROR_LEN,
    _connect,
    _parse_dt,
    _to_db_dt,
)


def _dump_refs(refs: tuple[str, ...]) -> str | None:
    if not refs:
        return None
    return json.dumps(list(refs), ensure_ascii=False)


def _load_refs(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(str(ref) for ref in json.loads(value))


def _dump_months(months: tuple[int, ...]) -> str | None:
    if not months:
        return None
    return json.dumps(list(months))


def _load_months(value: str | None) -> tuple[int, ...]:
    if not value:
        return ()
    return tuple(int(month) for month in json.loads(value))


def _row_to_run(row: sqlite3.Row) -> SlotRun:
    return SlotRun(
        id=row["id"],
        rubric_id=row["rubric_id"],
        slot_date=row["slot_date"],
        status=row["status"],
        skip_reason=row["skip_reason"],
        error=row["error"],
        attempts=row["attempts"],
        target=row["target"],
        is_dry_run=bool(row["is_dry_run"]),
        text=row["text"],
        image_ref=row["image_ref"],
        source_refs=_load_refs(row["source_refs"]),
        max_message_id=row["max_message_id"],
        created_at=_parse_dt(row["created_at"]),
        updated_at=_parse_dt(row["updated_at"]),
        published_at=_parse_dt(row["published_at"]),
    )


def _row_to_bank_item(row: sqlite3.Row) -> ContentBankItem:
    metadata = row["metadata"]
    return ContentBankItem(
        id=row["id"],
        item_type=row["item_type"],
        text=row["text"],
        metadata=json.loads(metadata) if metadata else {},
        review_status=row["review_status"],
        show_count=row["show_count"],
        last_shown_at=_parse_dt(row["last_shown_at"]),
        months=_load_months(row["months"]),
        event_date=row["event_date"],
        created_at=_parse_dt(row["created_at"]),
    )


def _row_to_digest_item(row: sqlite3.Row) -> DigestItemRef:
    return DigestItemRef(
        id=row["id"],
        slot_run_id=row["slot_run_id"],
        url=row["url"],
        source=row["source"],
        title=row["title"],
        position=row["position"],
        created_at=_parse_dt(row["created_at"]),
    )


def _row_to_filter_entry(row: sqlite3.Row) -> FilterLogEntry:
    return FilterLogEntry(
        id=row["id"],
        stage=row["stage"],
        decision=row["decision"],
        url=row["url"],
        title=row["title"],
        reason=row["reason"],
        model=row["model"],
        response=row["response"],
        created_at=_parse_dt(row["created_at"]),
    )


class SlotRunRepository:
    """slot_runs: one rubric slot per day, UNIQUE(rubric_id, slot_date)."""

    def __init__(self, path: Path) -> None:
        self._path = path

    async def try_claim(
        self,
        rubric_id: str,
        slot_date: str,
        *,
        target: str | None = None,
        is_dry_run: bool = False,
    ) -> SlotRun | None:
        """Insert a pending run, claiming the slot for the day.

        Returns the created run, or None when the slot is already taken
        (UNIQUE constraint) — the scheduler treats that as "already
        handled" and skips re-running the rubric.
        """
        return await asyncio.to_thread(
            self._try_claim_sync, rubric_id, slot_date, target, is_dry_run
        )

    def _try_claim_sync(
        self,
        rubric_id: str,
        slot_date: str,
        target: str | None,
        is_dry_run: bool,
    ) -> SlotRun | None:
        with _connect(self._path) as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO slot_runs (rubric_id, slot_date, target, is_dry_run)
                    VALUES (?, ?, ?, ?)
                    """,
                    (rubric_id, slot_date, target, int(is_dry_run)),
                )
            except sqlite3.IntegrityError:
                return None
            row = conn.execute(
                """
                SELECT * FROM slot_runs
                WHERE rubric_id = ? AND slot_date = ?
                """,
                (rubric_id, slot_date),
            ).fetchone()
            conn.commit()
            assert row is not None  # inserted a row above
            return _row_to_run(row)

    async def get(self, rubric_id: str, slot_date: str) -> SlotRun | None:
        return await asyncio.to_thread(self._get_by_keys_sync, rubric_id, slot_date)

    def _get_by_keys_sync(
        self, rubric_id: str, slot_date: str
    ) -> SlotRun | None:
        with _connect(self._path) as conn:
            row = conn.execute(
                """
                SELECT * FROM slot_runs
                WHERE rubric_id = ? AND slot_date = ?
                """,
                (rubric_id, slot_date),
            ).fetchone()
            return None if row is None else _row_to_run(row)

    async def save(self, run: SlotRun) -> None:
        """Persist every mutable field of an already-claimed run.

        Status transitions belong to the caller (the scheduler); the
        database CHECK keeps the status vocabulary honest.
        """
        await asyncio.to_thread(self._save_sync, run)

    def _save_sync(self, run: SlotRun) -> None:
        if run.id is None:
            raise ValueError("SlotRun must be claimed (try_claim) before save")
        published_at = (
            _to_db_dt(run.published_at) if run.published_at is not None else None
        )
        with _connect(self._path) as conn:
            conn.execute(
                """
                UPDATE slot_runs SET
                    status = ?,
                    skip_reason = ?,
                    error = ?,
                    attempts = ?,
                    target = ?,
                    is_dry_run = ?,
                    text = ?,
                    image_ref = ?,
                    source_refs = ?,
                    max_message_id = ?,
                    published_at = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (
                    run.status,
                    run.skip_reason,
                    run.error[:_MAX_ERROR_LEN] if run.error else None,
                    run.attempts,
                    run.target,
                    int(run.is_dry_run),
                    run.text,
                    run.image_ref,
                    _dump_refs(run.source_refs),
                    run.max_message_id,
                    published_at,
                    run.id,
                ),
            )
            conn.commit()

    async def find_by_status(
        self, status: str, *, limit: int | None = None
    ) -> list[SlotRun]:
        """Runs in a given status, newest first (recovery scans these)."""
        return await asyncio.to_thread(self._find_by_status_sync, status, limit)

    async def list_for_date(self, slot_date: str) -> list[SlotRun]:
        """All runs of one slot date, in rubric-declaration order.

        The daily summary and /factory status read a whole day at once;
        ordering by insertion (id) keeps the rubrics config order stable.
        """
        return await asyncio.to_thread(self._list_for_date_sync, slot_date)

    def _list_for_date_sync(self, slot_date: str) -> list[SlotRun]:
        with _connect(self._path) as conn:
            cursor = conn.execute(
                "SELECT * FROM slot_runs WHERE slot_date = ? ORDER BY id ASC",
                (slot_date,),
            )
            return [_row_to_run(row) for row in cursor.fetchall()]

    async def get_by_id(self, run_id: int) -> SlotRun | None:
        return await asyncio.to_thread(self._get_by_id_sync, run_id)

    def _get_by_id_sync(self, run_id: int) -> SlotRun | None:
        with _connect(self._path) as conn:
            row = conn.execute(
                "SELECT * FROM slot_runs WHERE id = ?", (run_id,)
            ).fetchone()
            return None if row is None else _row_to_run(row)

    async def count_for_date(
        self,
        slot_date: str,
        *,
        statuses: Sequence[str] | None = None,
        dry_run: bool | None = None,
    ) -> int:
        """Runs on a slot date, optionally restricted to statuses.

        The scheduler's daily_cap counts runs that did produce or may still
        produce a post (pending/generated/publishing/published/...);
        skipped and failed runs published nothing and stay free. ``dry_run``
        narrows the count to preview runs (True) or channel runs (False):
        dry-run previews go to the admins, never to the channel, and must
        not consume the cap.
        """
        return await asyncio.to_thread(
            self._count_for_date_sync, slot_date, statuses, dry_run
        )

    def _count_for_date_sync(
        self,
        slot_date: str,
        statuses: Sequence[str] | None,
        dry_run: bool | None,
    ) -> int:
        conditions = ["slot_date = ?"]
        params: list = [slot_date]
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            conditions.append(f"status IN ({placeholders})")
            params.extend(statuses)
        if dry_run is not None:
            conditions.append("is_dry_run = ?")
            params.append(int(dry_run))
        with _connect(self._path) as conn:
            row = conn.execute(
                f"SELECT COUNT(*) FROM slot_runs WHERE {' AND '.join(conditions)}",
                params,
            ).fetchone()
            return int(row[0])

    def _find_by_status_sync(
        self, status: str, limit: int | None
    ) -> list[SlotRun]:
        with _connect(self._path) as conn:
            cursor = conn.execute(
                """
                SELECT * FROM slot_runs
                WHERE status = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (status, -1 if limit is None else limit),
            )
            return [_row_to_run(row) for row in cursor.fetchall()]

    async def recent_texts(self, rubric_id: str, limit: int) -> list[str]:
        """Newest published texts of one rubric, oldest first.

        Anti-repeat history for the generative pipeline prompt (plan section
        3): the last ``history_n`` posts the audience actually saw. Only
        published runs count - failed or skipped attempts showed nothing.
        """
        return await asyncio.to_thread(self._recent_texts_sync, rubric_id, limit)

    def _recent_texts_sync(self, rubric_id: str, limit: int) -> list[str]:
        with _connect(self._path) as conn:
            cursor = conn.execute(
                """
                SELECT text FROM slot_runs
                WHERE rubric_id = ?
                  AND status = 'published'
                  AND text IS NOT NULL
                ORDER BY slot_date DESC, id DESC
                LIMIT ?
                """,
                (rubric_id, limit),
            )
            texts = [str(row["text"]) for row in cursor.fetchall()]
        return list(reversed(texts))


class DigestItemsRepository:
    """digest_items: which news went into which digest issue."""

    def __init__(self, path: Path) -> None:
        self._path = path

    async def add(self, slot_run_id: int, items: Sequence[NewsItem]) -> None:
        """Record issue contents in list order (position 0..n-1).

        UNIQUE(slot_run_id, url) makes re-adding the same issue idempotent.
        """
        await asyncio.to_thread(self._add_sync, slot_run_id, list(items))

    def _add_sync(self, slot_run_id: int, items: list[NewsItem]) -> None:
        with _connect(self._path) as conn:
            for position, item in enumerate(items):
                conn.execute(
                    """
                    INSERT OR IGNORE INTO digest_items (
                        slot_run_id, url, source, title, position
                    )
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (slot_run_id, item.url, item.source, item.title, position),
                )
            conn.commit()

    async def has_url(self, url: str) -> bool:
        """True when the url already appeared in an earlier issue.

        The repeat guard of the digest filter chain (plan section 5,
        "отсутствие повторов"): the pipeline stores canonical urls, so the
        caller passes the canonical form too.
        """
        return await asyncio.to_thread(self._has_url_sync, url)

    def _has_url_sync(self, url: str) -> bool:
        with _connect(self._path) as conn:
            cursor = conn.execute(
                "SELECT 1 FROM digest_items WHERE url = ? LIMIT 1", (url,)
            )
            return cursor.fetchone() is not None

    async def list_for_run(self, slot_run_id: int) -> list[DigestItemRef]:
        return await asyncio.to_thread(self._list_for_run_sync, slot_run_id)

    def _list_for_run_sync(self, slot_run_id: int) -> list[DigestItemRef]:
        with _connect(self._path) as conn:
            cursor = conn.execute(
                """
                SELECT * FROM digest_items
                WHERE slot_run_id = ?
                ORDER BY position ASC, id ASC
                """,
                (slot_run_id,),
            )
            return [_row_to_digest_item(row) for row in cursor.fetchall()]


class RubricStateRepository:
    """rubric_state: pauses and target overrides surviving a restart."""

    def __init__(self, path: Path) -> None:
        self._path = path

    async def get(self, rubric_id: str) -> RubricState:
        """Absent state means "not paused, default target"."""
        return await asyncio.to_thread(self._get_sync, rubric_id)

    def _get_sync(self, rubric_id: str) -> RubricState:
        with _connect(self._path) as conn:
            row = conn.execute(
                "SELECT * FROM rubric_state WHERE rubric_id = ?", (rubric_id,)
            ).fetchone()
            if row is None:
                return RubricState(rubric_id=rubric_id)
            return RubricState(
                rubric_id=row["rubric_id"],
                paused=bool(row["paused"]),
                target=row["target"],
                updated_at=_parse_dt(row["updated_at"]),
            )

    async def set_paused(self, rubric_id: str, paused: bool) -> None:
        await asyncio.to_thread(self._set_paused_sync, rubric_id, paused)

    def _set_paused_sync(self, rubric_id: str, paused: bool) -> None:
        with _connect(self._path) as conn:
            conn.execute(
                """
                INSERT INTO rubric_state (rubric_id, paused, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(rubric_id) DO UPDATE SET
                    paused = excluded.paused,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (rubric_id, int(paused)),
            )
            conn.commit()

    async def set_target(self, rubric_id: str, target: str | None) -> None:
        await asyncio.to_thread(self._set_target_sync, rubric_id, target)

    def _set_target_sync(self, rubric_id: str, target: str | None) -> None:
        with _connect(self._path) as conn:
            conn.execute(
                """
                INSERT INTO rubric_state (rubric_id, target, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(rubric_id) DO UPDATE SET
                    target = excluded.target,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (rubric_id, target),
            )
            conn.commit()


class ContentBankRepository:
    """content_bank (plan §6): reviewed items with seasonality and show
    counters. Selection *policy* (season config, depletion alerts) belongs
    to the rubrics layer; this class only stores and filters."""

    def __init__(self, path: Path) -> None:
        self._path = path

    async def add(self, item: ContentBankItem) -> int:
        """Insert one item and return its row id.

        A duplicate (item_type, text) is ignored and the existing row id is
        returned, so an import batch can be re-run safely.
        """
        return await asyncio.to_thread(self._add_sync, item)

    def _add_sync(self, item: ContentBankItem) -> int:
        with _connect(self._path) as conn:
            conn.execute(
                """
                INSERT INTO content_bank (
                    item_type, text, metadata, review_status,
                    show_count, last_shown_at, months, event_date
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(item_type, text) DO NOTHING
                """,
                (
                    item.item_type,
                    item.text,
                    (
                        json.dumps(item.metadata, ensure_ascii=False)
                        if item.metadata
                        else None
                    ),
                    item.review_status,
                    item.show_count,
                    (
                        _to_db_dt(item.last_shown_at)
                        if item.last_shown_at is not None
                        else None
                    ),
                    _dump_months(item.months),
                    item.event_date,
                ),
            )
            row = conn.execute(
                "SELECT id FROM content_bank WHERE item_type = ? AND text = ?",
                (item.item_type, item.text),
            ).fetchone()
            conn.commit()
            return int(row[0])

    async def get(self, item_id: int) -> ContentBankItem | None:
        return await asyncio.to_thread(self._get_sync, item_id)

    def _get_sync(self, item_id: int) -> ContentBankItem | None:
        with _connect(self._path) as conn:
            row = conn.execute(
                "SELECT * FROM content_bank WHERE id = ?", (item_id,)
            ).fetchone()
            return None if row is None else _row_to_bank_item(row)

    async def list_available(
        self,
        item_type: str,
        *,
        month: int | None = None,
        event_date: str | None = None,
    ) -> list[ContentBankItem]:
        """Approved items of a type, in insertion order.

        event_date is filtered in SQL; month (seasonality) is matched in
        Python against the JSON months array — empty months means "any
        month". Both filters are None-safe.
        """
        return await asyncio.to_thread(
            self._list_available_sync, item_type, month, event_date
        )

    def _list_available_sync(
        self, item_type: str, month: int | None, event_date: str | None
    ) -> list[ContentBankItem]:
        with _connect(self._path) as conn:
            cursor = conn.execute(
                """
                SELECT * FROM content_bank
                WHERE item_type = ?
                  AND review_status = 'approved'
                  AND (? IS NULL OR event_date = ?)
                ORDER BY id ASC
                """,
                (item_type, event_date, event_date),
            )
            items = [_row_to_bank_item(row) for row in cursor.fetchall()]
        if month is None:
            return items
        return [
            item for item in items if not item.months or month in item.months
        ]

    async def mark_shown(self, item_id: int) -> None:
        """Bump the show counter and stamp the last shown time (UTC)."""
        await asyncio.to_thread(self._mark_shown_sync, item_id)

    def _mark_shown_sync(self, item_id: int) -> None:
        with _connect(self._path) as conn:
            conn.execute(
                """
                UPDATE content_bank SET
                    show_count = show_count + 1,
                    last_shown_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (item_id,),
            )
            conn.commit()

    async def set_review_status(self, item_id: int, status: str) -> None:
        await asyncio.to_thread(self._set_review_status_sync, item_id, status)

    def _set_review_status_sync(self, item_id: int, status: str) -> None:
        with _connect(self._path) as conn:
            conn.execute(
                "UPDATE content_bank SET review_status = ? WHERE id = ?",
                (status, item_id),
            )
            conn.commit()

    async def count_available(self, item_type: str) -> int:
        """Approved items of a type left in the bank (stock alerts)."""
        return await asyncio.to_thread(self._count_available_sync, item_type)

    def _count_available_sync(self, item_type: str) -> int:
        with _connect(self._path) as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) FROM content_bank
                WHERE item_type = ? AND review_status = 'approved'
                """,
                (item_type,),
            ).fetchone()
            return int(row[0])

    async def count_available_by_type(self) -> dict[str, int]:
        """Approved counts for every bank type (daily summary stock report)."""
        return await asyncio.to_thread(self._count_available_by_type_sync)

    def _count_available_by_type_sync(self) -> dict[str, int]:
        with _connect(self._path) as conn:
            cursor = conn.execute(
                """
                SELECT item_type, COUNT(*) FROM content_bank
                WHERE review_status = 'approved'
                GROUP BY item_type
                """
            )
            return {str(row[0]): int(row[1]) for row in cursor.fetchall()}


class FilterLogRepository:
    """filter_log: classifier/checker answers and rejection reasons."""

    def __init__(self, path: Path) -> None:
        self._path = path

    async def log(self, entry: FilterLogEntry) -> None:
        await asyncio.to_thread(self._log_sync, entry)

    def _log_sync(self, entry: FilterLogEntry) -> None:
        with _connect(self._path) as conn:
            conn.execute(
                """
                INSERT INTO filter_log (
                    stage, decision, url, title, reason, model, response
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry.stage,
                    entry.decision,
                    entry.url,
                    entry.title,
                    entry.reason,
                    entry.model,
                    entry.response,
                ),
            )
            conn.commit()

    async def recent(self, limit: int = 50) -> list[FilterLogEntry]:
        return await asyncio.to_thread(self._recent_sync, limit)

    def _recent_sync(self, limit: int) -> list[FilterLogEntry]:
        with _connect(self._path) as conn:
            cursor = conn.execute(
                """
                SELECT * FROM filter_log
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            )
            return [_row_to_filter_entry(row) for row in cursor.fetchall()]

    async def count_between(self, start: datetime, end: datetime) -> int:
        """Filter decisions in a UTC half-open interval [start, end).

        The daily summary counts classifier/checker LLM calls of the local
        day this way (created_at is UTC, the local day is not).
        """
        return await asyncio.to_thread(self._count_between_sync, start, end)

    def _count_between_sync(self, start: datetime, end: datetime) -> int:
        with _connect(self._path) as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) FROM filter_log
                WHERE created_at >= ? AND created_at < ?
                """,
                (_to_db_dt(start), _to_db_dt(end)),
            ).fetchone()
            return int(row[0])


class ShadowNewsRepository:
    """Stage-0 shadow collection store (plan section 14): seen_news +
    shadow_runs in a *separate* sqlite file.

    The shadow run must not touch the production dedup tables (sent_news,
    posts), otherwise fresh news would be discarded as duplicates once the
    test week starts. ``seen_news`` mirrors ``sent_news`` (url UNIQUE,
    source, title, stamp) plus the fields the digest stage consumes
    (section, published_at, summary); ``shadow_runs`` logs one row per
    collection pass for the operator review of the 2-3 shadow days. The
    schema is created here, not by the main-database migrations: the file
    is standalone and disposable once the test week begins.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        """Shadow database file path (kept apart from the main database)."""
        return self._path

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize_sync)

    def _initialize_sync(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with _connect(self._path) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS seen_news (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    url TEXT UNIQUE NOT NULL,
                    source TEXT NOT NULL,
                    title TEXT NOT NULL,
                    section TEXT,
                    published_at TIMESTAMP,
                    summary TEXT,
                    first_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_seen_news_first_seen
                ON seen_news (first_seen_at)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS shadow_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    feeds INTEGER NOT NULL,
                    collected INTEGER NOT NULL,
                    stored INTEGER NOT NULL,
                    duplicates INTEGER NOT NULL,
                    malformed INTEGER NOT NULL DEFAULT 0,
                    empty_feeds INTEGER NOT NULL DEFAULT 0,
                    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.commit()

    async def is_seen(self, url: str) -> bool:
        return await asyncio.to_thread(self._is_seen_sync, url)

    def _is_seen_sync(self, url: str) -> bool:
        with _connect(self._path) as conn:
            cursor = conn.execute(
                "SELECT 1 FROM seen_news WHERE url = ? LIMIT 1", (url,)
            )
            return cursor.fetchone() is not None

    async def mark_seen(self, item: NewsItem) -> None:
        await asyncio.to_thread(self._mark_seen_sync, item)

    def _mark_seen_sync(self, item: NewsItem) -> None:
        published_at = (
            _to_db_dt(item.published_at) if item.published_at is not None else None
        )
        with _connect(self._path) as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO seen_news (
                    url, source, title, section, published_at, summary
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    item.url,
                    item.source,
                    item.title,
                    item.category,
                    published_at,
                    item.summary,
                ),
            )
            conn.commit()

    async def count_seen(self) -> int:
        return await asyncio.to_thread(self._count_seen_sync)

    def _count_seen_sync(self) -> int:
        with _connect(self._path) as conn:
            row = conn.execute("SELECT COUNT(*) FROM seen_news").fetchone()
            return int(row[0])

    async def recent_items(self, limit: int = 50) -> list[NewsItem]:
        """Newest stored items first (shadow-period review material)."""
        return await asyncio.to_thread(self._recent_items_sync, limit)

    def _recent_items_sync(self, limit: int) -> list[NewsItem]:
        with _connect(self._path) as conn:
            cursor = conn.execute(
                """
                SELECT * FROM seen_news
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            )
            return [
                NewsItem(
                    title=str(row["title"]),
                    url=str(row["url"]),
                    source=str(row["source"]),
                    published_at=_parse_dt(row["published_at"]),
                    category=row["section"],
                    summary=row["summary"],
                )
                for row in cursor.fetchall()
            ]

    async def log_run(
        self,
        *,
        feeds: int,
        collected: int,
        stored: int,
        duplicates: int,
        malformed: int = 0,
        empty_feeds: int = 0,
    ) -> None:
        await asyncio.to_thread(
            self._log_run_sync, feeds, collected, stored, duplicates,
            malformed, empty_feeds,
        )

    def _log_run_sync(
        self,
        feeds: int,
        collected: int,
        stored: int,
        duplicates: int,
        malformed: int,
        empty_feeds: int,
    ) -> None:
        with _connect(self._path) as conn:
            conn.execute(
                """
                INSERT INTO shadow_runs (
                    feeds, collected, stored, duplicates, malformed, empty_feeds
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (feeds, collected, stored, duplicates, malformed, empty_feeds),
            )
            conn.commit()

    async def recent_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        """Latest collection passes, newest first (shadow-period review)."""
        return await asyncio.to_thread(self._recent_runs_sync, limit)

    def _recent_runs_sync(self, limit: int) -> list[dict[str, Any]]:
        with _connect(self._path) as conn:
            cursor = conn.execute(
                "SELECT * FROM shadow_runs ORDER BY id DESC LIMIT ?", (limit,)
            )
            return [
                {
                    "id": row["id"],
                    "feeds": row["feeds"],
                    "collected": row["collected"],
                    "stored": row["stored"],
                    "duplicates": row["duplicates"],
                    "malformed": row["malformed"],
                    "empty_feeds": row["empty_feeds"],
                    "started_at": row["started_at"],
                }
                for row in cursor.fetchall()
            ]
