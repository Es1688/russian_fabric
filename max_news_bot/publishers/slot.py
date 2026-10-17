"""Slot publisher for the rubrics contour (plan sections 8, 12).

Implements the ``SlotPublisher`` protocol of ``bot/slot_scheduler.py``:
one rubric post to the channel (or, in dry-run, previews to the admins),
optionally carrying a postcard image from the content bank. The card is
uploaded to MAX through the two-step ``POST /uploads`` scheme and attached
by its mediafile token (research report section 1).

Degradation policy: a *local* card problem (unresolved image_ref, missing
file, bad extension) drops the card and sends the text alone - the plan's
image basis is the bank, and a broken card file must not fail a 07:00
slot. A *transport* failure (``/uploads`` down, rate limited) fails the
publish as infra instead: the scheduler keeps the generated text with its
image_ref and retries the whole publish after the global pause, so the
post is never silently sent without its card because MAX was flaky.

Generated slot images (``generated:<file>`` refs of ``image: generate``
rubrics, dual-channel plan §3.3) follow the same policy and are deleted
after the confirmed publish: until then the file stays on disk for
publish retries, after it the daily backup TTL prunes crash leftovers.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from max_news_bot.bank.postcards import (
    PostcardError,
    is_concrete_ref,
    load_postcard_bytes,
    resolve_postcard,
)
from max_news_bot.bot.slot_scheduler import SlotPublishResult
from max_news_bot.max_api.client import MaxClient
from max_news_bot.max_api.errors import (
    MaxApiError,
    MaxRateLimitError,
    classify_error,
    is_attachment_not_ready,
)
from max_news_bot.max_api.uploads import MaxMediaUploader
from max_news_bot.utils.logging import get_logger
from max_news_bot.utils.repositories import ContentBankRepository
from max_news_bot.utils.sanitize import sanitize_html


logger = get_logger(__name__)

_MAX_ERROR_LEN = 300
_MAX_SEND_ATTEMPTS = 3  # total tries per message on rate limiting
# attachment.not.ready: the server processes uploads asynchronously, docs
# recommend a pause with a growing interval (research report section 1.4)
_MAX_ATTACHMENT_READY_ATTEMPTS = 4

# generated slot image refs ("generated:<file>", rubrics/generative.py);
# the files are plain names inside one dedicated directory
_GENERATED_REF_PREFIX = "generated:"
# same MAX-accepted image set as bank/postcards.py (kept local: the bank
# module speaks bank records, not generated files)
_GENERATED_CONTENT_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".heic": "image/heic",
}


def image_attachment(token: str) -> dict[str, Any]:
    """The /messages attachment payload for an uploaded image."""
    return {"type": "image", "payload": {"token": token}}


def _is_bare_name(name: str) -> bool:
    """True for a single plain path component (the postcards guard,
    stricter: generated refs never carry subdirectories)."""
    path = Path(name)
    return not path.is_absolute() and ".." not in path.parts and len(path.parts) == 1


class MaxSlotPublisher:
    """Sends rubric posts to MAX with an optional bank postcard."""

    # pause between consecutive MAX sends: the rate limit is 2 messages/sec
    # per dialog; tests shrink it to zero (same as MaxPostPublisher)
    SEND_PAUSE_SECONDS: float = 2.5
    # base pause of the growing attachment.not.ready interval: 1s, 2s, 4s
    ATTACHMENT_PAUSE_SECONDS: float = 1.0

    def __init__(
        self,
        client: MaxClient,
        admin_ids: list[int],
        *,
        bank: ContentBankRepository,
        postcards_dir: Path,
        generated_dir: Path | None = None,
    ) -> None:
        self._client = client
        self._admin_ids = list(admin_ids)
        self._bank = bank
        self._postcards_dir = postcards_dir
        self._generated_dir = generated_dir
        self._uploader = MaxMediaUploader(client)
        self._sends = 0

    async def publish_slot(
        self,
        text: str,
        *,
        chat_id: int,
        image_ref: str | None = None,
        to_admins: bool = False,
    ) -> SlotPublishResult:
        if not text or not text.strip():
            return SlotPublishResult(
                success=False,
                error="slot text is empty",
                error_kind="content",
            )
        attachments: list[dict[str, Any]] | None = None
        is_generated_ref = bool(
            image_ref and image_ref.strip().startswith(_GENERATED_REF_PREFIX)
        )
        if image_ref and image_ref.strip():
            try:
                attachments = await self._postcard_attachments(image_ref)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # upload/transport failure: infra error, no text-only send
                return self._error(exc)
        message = sanitize_html(text)
        if to_admins:
            result = await self._publish_to_admins(message, attachments)
        else:
            result = await self._publish_to_channel(chat_id, message, attachments)
        if result.success and is_generated_ref:
            # confirmed publish (retries already happened inside): the
            # image is gone from the message, so is the file
            await self._delete_generated_image(image_ref)
        return result

    # --- postcard ---------------------------------------------------------------

    async def _postcard_attachments(
        self, image_ref: str
    ) -> list[dict[str, Any]] | None:
        """Attachment payload for the postcard, or None to go text-only.

        Local card problems degrade to None with a warning; transport
        errors propagate to ``publish_slot`` and fail the publish as
        infra. A bare type ref (``rubric.image`` of a generative rubric)
        selects the least-shown card here and rotates the stock; a
        concrete ``bank:<type>/<id>`` ref was already marked shown by the
        bank pipeline that picked it. ``generated:<file>`` refs are read
        from the generated-images directory instead of the bank."""
        if image_ref.strip().startswith(_GENERATED_REF_PREFIX):
            return await self._generated_attachments(image_ref)
        item = await resolve_postcard(self._bank, image_ref)
        if item is None:
            logger.warning(
                "slot_postcard_unresolved image_ref=%s image=none", image_ref
            )
            return None
        if not is_concrete_ref(image_ref) and item.id is not None:
            await self._bank.mark_shown(item.id)
        try:
            data, filename, content_type = await load_postcard_bytes(
                item, self._postcards_dir
            )
        except PostcardError as exc:
            logger.warning(
                "slot_postcard_unreadable image_ref=%s error=%s", image_ref, exc
            )
            return None
        token = await self._uploader.upload_image(data, filename, content_type)
        return [image_attachment(token)]

    async def _generated_attachments(
        self, image_ref: str
    ) -> list[dict[str, Any]] | None:
        """Attachment payload for a generated slot image, or None for text-only.

        Reads ``generated:<file>`` from the generated-images directory
        under the same path-traversal guard as the bank postcards; a
        missing file, an escaping reference or a non-image extension is a
        local problem and degrades to None, while upload/transport errors
        propagate as infra (dual-channel plan §3.4)."""
        name = image_ref.strip()[len(_GENERATED_REF_PREFIX) :].strip()
        if self._generated_dir is None or not _is_bare_name(name):
            logger.warning(
                "slot_generated_ref_rejected image_ref=%s image=none", image_ref
            )
            return None
        content_type = _GENERATED_CONTENT_TYPES.get(Path(name).suffix.lower())
        if content_type is None:
            logger.warning(
                "slot_generated_bad_extension image_ref=%s image=none", image_ref
            )
            return None
        full_path = self._generated_dir / name
        try:
            data = await asyncio.to_thread(full_path.read_bytes)
        except OSError as exc:
            logger.warning(
                "slot_generated_unreadable image_ref=%s error=%s", image_ref, exc
            )
            return None
        token = await self._uploader.upload_image(data, name, content_type)
        return [image_attachment(token)]

    async def _delete_generated_image(self, image_ref: str) -> None:
        """Best-effort removal after a confirmed publish (plan §3.3).

        A failed unlink never fails the already-successful publish: the
        daily backup TTL-prune is the safety net for leftovers."""
        name = image_ref.strip()[len(_GENERATED_REF_PREFIX) :].strip()
        if self._generated_dir is None or not _is_bare_name(name):
            return
        full_path = self._generated_dir / name
        try:
            await asyncio.to_thread(full_path.unlink)
            logger.info("generated_image_deleted path=%s", full_path)
        except OSError as exc:
            logger.warning(
                "generated_image_delete_failed path=%s error=%s", full_path, exc
            )

    # --- sending ------------------------------------------------------------------

    async def _publish_to_channel(
        self,
        chat_id: int,
        message: str,
        attachments: list[dict[str, Any]] | None,
    ) -> SlotPublishResult:
        try:
            message_id = await self._send_with_retries(
                chat_id, message, attachments
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return self._error(exc)
        return SlotPublishResult(success=True, message_id=message_id)

    async def _publish_to_admins(
        self, message: str, attachments: list[dict[str, Any]] | None
    ) -> SlotPublishResult:
        preview = "[DRY-RUN] " + message
        delivered = 0
        for admin_id in self._admin_ids:
            try:
                # admin_ids hold user_id: DMs are addressed by user_id
                await self._send_with_retries(
                    None, preview, attachments, user_id=admin_id
                )
                delivered += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # one admin failing must not block the other previews
                logger.error(
                    "admin_preview_failed admin_id=%s error=%s", admin_id, exc
                )
        if not delivered:
            return SlotPublishResult(
                success=False,
                error=f"no admin preview delivered ({len(self._admin_ids)} admins)",
                error_kind="infra",
            )
        return SlotPublishResult(success=True)

    async def _send_with_retries(
        self,
        chat_id: int | None,
        message: str,
        attachments: list[dict[str, Any]] | None,
        *,
        user_id: int | None = None,
    ) -> str:
        """send_message with rate-limit and attachment.not.ready retries.

        The not-ready loop wraps the rate-limit loop: each not-ready retry
        re-sends the SAME message with the SAME token - the file keeps
        processing server-side, no re-upload is needed. Addressing passes
        through to the client: channel by chat_id, admin DMs by user_id."""
        not_ready = 0
        while True:
            try:
                return await self._send_rate_limited(
                    chat_id, message, attachments, user_id=user_id
                )
            except MaxApiError as exc:
                if (
                    not is_attachment_not_ready(exc)
                    or not_ready >= _MAX_ATTACHMENT_READY_ATTEMPTS - 1
                ):
                    raise
                not_ready += 1
                pause = self.ATTACHMENT_PAUSE_SECONDS * (2 ** (not_ready - 1))
                logger.warning(
                    "attachment not ready, sleeping %ss (attempt %s/%s) error=%s",
                    pause,
                    not_ready + 1,
                    _MAX_ATTACHMENT_READY_ATTEMPTS,
                    exc,
                )
                await asyncio.sleep(pause)

    async def _send_rate_limited(
        self,
        chat_id: int | None,
        message: str,
        attachments: list[dict[str, Any]] | None,
        *,
        user_id: int | None = None,
    ) -> str:
        for attempt in range(1, _MAX_SEND_ATTEMPTS + 1):
            if self._sends > 0:
                await asyncio.sleep(self.SEND_PAUSE_SECONDS)
            try:
                mid = await self._client.send_message(
                    chat_id,
                    message,
                    user_id=user_id,
                    fmt="html",
                    attachments=attachments,
                )
                self._sends += 1
                return mid
            except MaxRateLimitError as exc:
                if attempt == _MAX_SEND_ATTEMPTS:
                    raise
                # MAX gives no retry_after: back off with a fixed sleep
                backoff = attempt * self.SEND_PAUSE_SECONDS
                logger.warning(
                    "rate limited, sleeping %ss (attempt %s/%s) error=%s",
                    backoff,
                    attempt,
                    _MAX_SEND_ATTEMPTS,
                    exc,
                )
                await asyncio.sleep(backoff)
        raise AssertionError("unreachable")

    def _error(self, exc: Exception) -> SlotPublishResult:
        message = f"{type(exc).__name__}: {exc}"
        kind = classify_error(exc)
        logger.error("slot_publish_failed kind=%s error=%s", kind, message)
        return SlotPublishResult(
            success=False,
            error=message[:_MAX_ERROR_LEN],
            error_kind=kind,
        )
