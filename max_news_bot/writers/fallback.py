"""Per-request LLM completion failover (plan §2.3, §2.6).

No sticky state: every call starts with the primary; an
``LlmCompletionError`` of any kind switches to the fallback. The prompt is
provider-agnostic, so both channels receive the same one — the wrapper
never touches it.

Quiet failover must not hide a degrading primary: every call leaves one
durable bot_stats event (``llm_primary_ok`` / ``llm_fallback_used`` /
``llm_<channel>_error_<kind>``, channel = primary|fallback, details carry
the context) and a config error of ANY channel fires an immediate admin
alert — even when a successful fallback masks it. Alert dedup lives in
process memory, at most once per day per channel: a restart may re-alert
a still-broken channel on purpose, the problem did not go away.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol

from max_news_bot.utils.db import Database
from max_news_bot.utils.logging import get_logger
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
    """The MaxAlerter surface the wrapper needs (MockAlerter fits too)."""

    async def send_alert(self, text: str) -> None: ...


class _ChannelObserver:
    """bot_stats events plus per-channel config-error alerting.

    Both collaborators are optional so the wrapper stays usable in
    contours without a database or alerting. Observability must never
    break the call: event and alert failures are logged and swallowed.
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
