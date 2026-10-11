"""Postcard (image) records of the content bank (plan section 12).

The image basis of the channel is a set of pre-approved postcards in one
style; YandexART generation is NOT wired (operator decision: bank mode).
A postcard is a ``content_bank`` row whose ``metadata`` carries the image
file path under ``image`` (relative to the channel's postcards directory).
The file itself never enters the database.

``image_ref`` of a slot run (plan section 9) points at the concrete
postcard used - ``bank:<item_type>/<id>``, matching the ``source_refs``
format - or carries a bare bank type name (``postcard``) when the
generative pipeline only knows the style name; both are resolved here.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

from max_news_bot.core.models import ContentBankItem
from max_news_bot.utils.logging import get_logger
from max_news_bot.utils.repositories import ContentBankRepository


logger = get_logger(__name__)

# metadata key holding the image file path relative to the postcards dir
IMAGE_METADATA_KEY = "image"

# MAX accepts JPG/JPEG/PNG/GIF/TIFF/BMP/HEIC images (research report
# section 1.2); anything else is rejected client-side with a clear error
# instead of the platform's "File extension is forbidden".
_CONTENT_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".heic": "image/heic",
}

_CONCRETE_REF_RE = re.compile(r"^bank:([a-z0-9_]+)/(\d+)$")


class PostcardError(ValueError):
    """A postcard record or its image file is unusable."""


def has_image(item: ContentBankItem) -> bool:
    """True when the bank record carries an image file reference."""
    ref = item.metadata.get(IMAGE_METADATA_KEY)
    return isinstance(ref, str) and bool(ref.strip())


def postcard_ref(item: ContentBankItem) -> str:
    """Concrete image_ref of a bank item: ``bank:<item_type>/<id>``."""
    if item.id is None:
        raise PostcardError("postcard item has no repository id")
    return f"bank:{item.item_type}/{item.id}"


async def select_postcard(
    bank: ContentBankRepository, item_type: str, *, month: int | None = None
) -> ContentBankItem | None:
    """Least-shown approved postcard of a bank type, or None.

    Only records with an image file count as postcards. Seasonality is
    applied only when the caller knows the local month; postcards are
    style-uniform, so a missing month must not filter them away."""
    items = await bank.list_available(item_type, month=month)
    postcards = [item for item in items if has_image(item)]
    if not postcards:
        return None
    return min(postcards, key=lambda item: (item.show_count, item.id or 0))


def is_concrete_ref(image_ref: str) -> bool:
    """True for concrete ``bank:<type>/<id>`` refs, False for bare names."""
    return _CONCRETE_REF_RE.match(image_ref.strip()) is not None


async def resolve_postcard(
    bank: ContentBankRepository,
    image_ref: str,
    *,
    month: int | None = None,
) -> ContentBankItem | None:
    """Resolve an image_ref to a bank postcard record, or None.

    ``bank:<type>/<id>`` loads the concrete record (type is verified);
    a bare name (e.g. ``postcard`` from ``rubric.image``) selects the
    least-shown postcard of that bank type.
    """
    match = _CONCRETE_REF_RE.match(image_ref.strip())
    if match is None:
        return await select_postcard(bank, image_ref.strip(), month=month)
    item_type, raw_id = match.groups()
    item = await bank.get(int(raw_id))
    if item is None or item.item_type != item_type or not has_image(item):
        return None
    return item


async def load_postcard_bytes(
    item: ContentBankItem, postcards_dir: Path
) -> tuple[bytes, str, str]:
    """Read the postcard file: (data, filename, content_type).

    Raises ``PostcardError`` when the record has no image, the reference
    escapes the postcards directory, the extension is not a MAX image
    type, or the file cannot be read - all content problems the caller
    degrades on (a broken card must not kill the slot), not infra ones.
    """
    ref = item.metadata.get(IMAGE_METADATA_KEY)
    if not isinstance(ref, str) or not ref.strip():
        raise PostcardError(f"bank item {item.id} has no image reference")
    path = Path(ref.strip())
    if path.is_absolute() or ".." in path.parts:
        raise PostcardError(f"image reference {ref!r} escapes the postcards dir")
    content_type = _CONTENT_TYPES.get(path.suffix.lower())
    if content_type is None:
        raise PostcardError(
            f"image {ref!r}: extension {path.suffix!r} is not a MAX image type"
        )
    full_path = postcards_dir / path
    try:
        data = await asyncio.to_thread(full_path.read_bytes)
    except OSError as exc:
        raise PostcardError(f"cannot read postcard {full_path}: {exc}") from exc
    return data, path.name, content_type
