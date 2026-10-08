from __future__ import annotations

import asyncio
import html
import re
from dataclasses import dataclass
from typing import Protocol

from max_news_bot.core.models import Post
from max_news_bot.max_api.client import MaxClient
from max_news_bot.max_api.errors import (
    MaxApiError,
    MaxRateLimitError,
    classify_error,
)
from max_news_bot.utils.logging import get_logger
from max_news_bot.utils.sanitize import sanitize_html


logger = get_logger(__name__)

_MAX_ERROR_LEN = 300
_MAX_SEND_ATTEMPTS = 3  # total tries per message on rate limiting
_TAG_RE = re.compile(r"</?[a-zA-Z][^>]*>")

# MAX has no parse_mode=None escape hatch: formats are html|markdown only,
# so the "plain" fallback is sent as html with every tag stripped and every
# character escaped — guaranteed-valid html either way
_PARSE_ERROR_MARKERS = ("parse", "format", "markup", "validation", "entities")


@dataclass(frozen=True)
class PostPublishResult:
    success: bool
    message_id: str | None
    error: str | None
    # "content" (text itself is unusable — burns an attempt) or "infra"
    # (network/flood — retried for free, plan P0.3); None means unknown/infra
    error_kind: str | None = None


class PostPublisherProtocol(Protocol):
    async def publish_post(
        self, post: Post, *, to_admins: bool = False
    ) -> PostPublishResult: ...


class MaxPostPublisher:
    """Publishes finished posts to the MAX channel (or admins in dry-run mode).

    The source link is appended by this code — the LLM never emits links.
    """

    # pause between consecutive MAX sends: the rate limit is 2 messages/sec
    # per dialog, and channel posts and admin copies share it; tests shrink
    # it to zero
    SEND_PAUSE_SECONDS: float = 2.5

    def __init__(
        self, client: MaxClient, chat_id: int, admin_ids: list[int]
    ) -> None:
        self._client = client
        self._chat_id = chat_id
        self._admin_ids = list(admin_ids)
        self._sends = 0

    async def publish_post(
        self, post: Post, *, to_admins: bool = False
    ) -> PostPublishResult:
        if not post.text or not post.text.strip():
            return PostPublishResult(
                success=False,
                message_id=None,
                error="post text is empty",
                error_kind="content",
            )

        if to_admins:
            return await self._publish_to_admins(post)
        return await self._publish_to_channel(post)

    async def _publish_to_channel(self, post: Post) -> PostPublishResult:
        text = sanitize_html(post.text)
        try:
            message_id = await self._send_with_retries(
                self._chat_id, _channel_message(text, post)
            )
        except MaxApiError as exc:
            if not _is_parse_error(exc):
                return self._error(exc)
            logger.warning("post html format failed, retrying plain: %s", exc)
            try:
                message_id = await self._send_with_retries(
                    self._chat_id, _channel_message(_plain_text(post.text), post)
                )
            except Exception as plain_exc:
                return self._error(plain_exc)
        except Exception as exc:
            return self._error(exc)
        return PostPublishResult(
            success=True, message_id=message_id, error=None, error_kind=None
        )

    async def _publish_to_admins(self, post: Post) -> PostPublishResult:
        text = sanitize_html(post.text)
        message = "[DRY-RUN] " + _channel_message(text, post)
        delivered = 0
        for admin_id in self._admin_ids:
            try:
                await self._send_with_retries(admin_id, message)
                delivered += 1
            except Exception as exc:
                # one admin failing must not block the other previews
                logger.error("admin_preview_failed admin_id=%s error=%s", admin_id, exc)
        if not delivered:
            return PostPublishResult(
                success=False,
                message_id=None,
                error=f"no admin preview delivered ({len(self._admin_ids)} admins)",
                error_kind="infra",
            )
        return PostPublishResult(
            success=True, message_id=None, error=None, error_kind=None
        )

    async def _send_with_retries(
        self, chat_id: int, message: str, *, fmt: str = "html"
    ) -> str:
        """send_message with rate-limit retry; returns the mid string."""
        for attempt in range(1, _MAX_SEND_ATTEMPTS + 1):
            if self._sends > 0:
                await asyncio.sleep(self.SEND_PAUSE_SECONDS)
            try:
                mid = await self._client.send_message(
                    chat_id, message, fmt=fmt, disable_link_preview=False
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

    def _error(self, exc: Exception) -> PostPublishResult:
        message = f"{type(exc).__name__}: {exc}"
        kind = _classify_send_error(exc)
        logger.error("post_publish_failed kind=%s error=%s", kind, message)
        return PostPublishResult(
            success=False,
            message_id=None,
            error=message[:_MAX_ERROR_LEN],
            error_kind=kind,
        )


def _channel_message(text: str, post: Post) -> str:
    # the source link is added by the code, never by the LLM; every MAX send
    # goes out with fmt="html", so "&" in the URL must always be escaped
    url = html.escape(post.news_url, quote=True)
    return f"{text}\nИсточник: {url}"


def _plain_text(text: str) -> str:
    # full fallback: tags stripped, everything escaped
    return html.escape(html.unescape(_TAG_RE.sub("", text)), quote=False)


def _is_parse_error(exc: MaxApiError) -> bool:
    """A 400 that blames the markup/format, not the length or the chat.

    MAX reports broken html as a plain 400 with a code/message hinting at
    parsing/validation; the plain variant would be just as long, so length
    complaints are not parse errors.
    """
    if exc.status != 400:
        return False
    haystack = f"{exc.code or ''} {exc.message}".lower()
    if _is_too_long(haystack):
        return False
    return any(marker in haystack for marker in _PARSE_ERROR_MARKERS)


def _is_too_long(haystack: str) -> bool:
    return "too long" in haystack or "too large" in haystack


def _classify_send_error(exc: Exception) -> str:
    """Infra send failures (network, flood control) must not burn attempts;
    content ones mean the text itself is unusable (plan P0.3). Delegated to
    max_api.errors.classify_error: a 400 is the message's own fault, auth/
    permissions/5xx and anything network-shaped are infra."""
    return classify_error(exc)
