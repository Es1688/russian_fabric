"""Per-request LLM failover wrappers (plan §2.3, §2.6).

No sticky state: every call starts with the primary; a WriteResult with
``status="error"`` (any error_kind) or an ``LlmCompletionError`` of any
kind switches to the fallback. The prompt is provider-agnostic, so both
channels receive the same one — the wrappers never touch it.

Quiet failover must not hide a degrading primary: every call leaves one
durable bot_stats event (``llm_primary_ok`` / ``llm_fallback_used`` /
``llm_<channel>_error_<kind>``, channel = primary|fallback, details carry
the context) and a config error of ANY channel fires an immediate admin
alert — even when a successful fallback masks it. Alert dedup lives in
process memory, at most once per day per channel: a restart may re-alert
a still-broken channel on purpose, the problem did not go away.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from typing import Protocol

from max_news_bot.core.models import NewsItem
from max_news_bot.utils.db import Database
from max_news_bot.utils.logging import get_logger
from max_news_bot.writers.base import Writer, WriteResult
from max_news_bot.writers.completion import (
    CompletionResult,
    LlmCompletion,
    LlmCompletionError,
)


logger = get_logger(__name__)

_MAX_ERROR_LEN = 300


def _truncate(text: str) -> str:
    return text[:_MAX_ERROR_LEN]


class Alerter(Protocol):
    """The MaxAlerter surface the wrappers need (MockAlerter fits too)."""

    async def send_alert(self, text: str) -> None: ...


class _ChannelObserver:
    """bot_stats events plus per-channel config-error alerting.

    Shared by both failover wrappers; both collaborators are optional so
    the wrappers stay usable in contours without a database or alerting.
    Observability must never break the call: event and alert failures are
    logged and swallowed.
    """

    def __init__(self, db: Database | None, alerter: Alerter | None) -> None:
        self._db = db
        self._alerter = alerter
        self._alerted_on: dict[str, str] = {}  # channel -> UTC date of last alert

    async def emit(self, event: str, details: str) -> None:
        if self._db is None:
            return
        try:
            await self._db.log_event(event, details)
        except Exception as exc:
            logger.error("llm_stats_write_failed event=%s error=%s", event, exc)

    async def alert_config_error(self, channel: str, error: str) -> None:
        if self._alerter is None:
            return
        today = datetime.now(timezone.utc).date().isoformat()
        if self._alerted_on.get(channel) == today:
            return
        try:
            await self._alerter.send_alert(
                f"LLM {channel} channel config error ({error}); "
                "check the key and billing. Calls go to the other channel."
            )
        except Exception as exc:
            # the dedup key stays unset: a failed delivery must not consume
            # the daily alert, the next config error retries
            logger.error("llm_alert_failed channel=%s error=%s", channel, exc)
            return
        self._alerted_on[channel] = today


class FallbackWriter:
    """Tries the primary writer, falls back on any of its errors.

    ``written`` / ``skipped`` from the primary return immediately; on an
    error result the fallback runs. When both fail, the fallback's
    WriteResult comes back with a combined error message while the
    error_kind stays the fallback's (a broken fallback key must still
    yield "config" so the factory auto-pauses). ``post.llm_model`` always
    names the channel that actually produced the post.
    """

    def __init__(
        self,
        primary: Writer,
        fallback: Writer,
        *,
        db: Database | None = None,
        alerter: Alerter | None = None,
    ) -> None:
        self._primary = primary
        self._fallback = fallback
        self._observer = _ChannelObserver(db, alerter)

    async def write_post(self, item: NewsItem) -> WriteResult:
        primary_result = await self._primary.write_post(item)
        if primary_result.status != "error":
            model = primary_result.post.llm_model if primary_result.post else None
            await self._observer.emit(
                "llm_primary_ok",
                f"model={model} outcome={primary_result.status}",
            )
            return primary_result
        return await self._via_fallback(item, primary_result)

    async def _via_fallback(
        self, item: NewsItem, primary_result: WriteResult
    ) -> WriteResult:
        logger.warning(
            "llm_primary_failed kind=%s error=%s",
            primary_result.error_kind,
            primary_result.error,
        )
        if primary_result.error_kind == "config":
            await self._observer.alert_config_error(
                "primary", primary_result.error or "unknown"
            )
        fallback_result = await self._fallback.write_post(item)
        if fallback_result.status != "error":
            model = (
                fallback_result.post.llm_model if fallback_result.post else None
            )
            await self._observer.emit(
                "llm_fallback_used",
                f"model={model} "
                f"primary_kind={primary_result.error_kind} "
                f"primary_error={primary_result.error}",
            )
            return fallback_result
        logger.warning(
            "llm_fallback_failed kind=%s error=%s",
            fallback_result.error_kind,
            fallback_result.error,
        )
        if fallback_result.error_kind == "config":
            await self._observer.alert_config_error(
                "fallback", fallback_result.error or "unknown"
            )
        await self._observer.emit(
            f"llm_fallback_error_{fallback_result.error_kind}",
            f"primary={primary_result.error}; fallback={fallback_result.error}",
        )
        # kind stays the fallback's; only the message becomes combined
        return replace(
            fallback_result,
            error=_truncate(
                f"primary={primary_result.error or 'unknown'}; "
                f"fallback={fallback_result.error or 'unknown'}"
            ),
        )


class FallbackCompletion:
    """Tries the primary completion client, falls back on any
    LlmCompletionError; the fallback's own error propagates as is."""

    def __init__(
        self,
        primary: LlmCompletion,
        fallback: LlmCompletion,
        *,
        db: Database | None = None,
        alerter: Alerter | None = None,
    ) -> None:
        self._primary = primary
        self._fallback = fallback
        self._observer = _ChannelObserver(db, alerter)

    async def complete(self, system: str, user: str) -> CompletionResult:
        try:
            result = await self._primary.complete(system, user)
        except LlmCompletionError as exc:
            return await self._via_fallback(system, user, exc)
        await self._observer.emit("llm_primary_ok", f"model={result.model}")
        return result

    async def _via_fallback(
        self, system: str, user: str, primary_error: LlmCompletionError
    ) -> CompletionResult:
        logger.warning(
            "llm_primary_failed kind=%s error=%s",
            primary_error.kind,
            primary_error,
        )
        if primary_error.kind == "config":
            await self._observer.alert_config_error("primary", str(primary_error))
        try:
            result = await self._fallback.complete(system, user)
        except LlmCompletionError as fallback_error:
            logger.warning(
                "llm_fallback_failed kind=%s error=%s",
                fallback_error.kind,
                fallback_error,
            )
            if fallback_error.kind == "config":
                await self._observer.alert_config_error(
                    "fallback", str(fallback_error)
                )
            await self._observer.emit(
                f"llm_fallback_error_{fallback_error.kind}",
                f"primary={primary_error}; fallback={fallback_error}",
            )
            raise
        await self._observer.emit(
            "llm_fallback_used",
            f"model={result.model} "
            f"primary_kind={primary_error.kind} "
            f"primary_error={primary_error}",
        )
        return result
