"""Per-request failover between image providers (plan §3.1, §2.6).

No sticky state: every call starts with the primary; an error of any kind
switches to the fallback when one is wired. Events are named after the
provider, not the slot — ``image_fal_ok`` means fal generated the image,
``image_art_used`` means Yandex did (also in an art-only wiring) — and
every call leaves one durable bot_stats event plus, on a config error of
ANY channel, an immediate admin alert (dedup: at most once per day per
channel). Observability never breaks the call: event and alert failures
are logged and swallowed.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol

from max_news_bot.images.base import ImageGenerationError, ImageGenerator
from max_news_bot.utils.db import Database
from max_news_bot.utils.logging import get_logger


logger = get_logger(__name__)

_MAX_ERROR_LEN = 300

# bot_stats event per provider (plan section 2.6); image_fal_ok predates
# the second provider, image_art_used is its reserved twin.
_PROVIDER_EVENTS = {"fal": "image_fal_ok", "yandex_art": "image_art_used"}


def _truncate(text: str) -> str:
    return text[:_MAX_ERROR_LEN]


def _event_for(provider: str) -> str:
    return _PROVIDER_EVENTS[provider]


class Alerter(Protocol):
    """The MaxAlerter surface the wrapper needs (MockAlerter fits too)."""

    async def send_alert(self, text: str) -> None: ...


class _ChannelObserver:
    """bot_stats events plus per-channel config-error alerting (the image
    twin of writers/fallback.py::_ChannelObserver).

    Both collaborators are optional so the wrapper stays usable in tests
    and contours without a database or alerting. Observability must never
    break the call: event and alert failures are logged and swallowed.
    """

    def __init__(self, db: Database | None, alerter: Alerter | None) -> None:
        self._db = db
        self._alerter = alerter
        self._alerted_on: dict[str, str] = {}  # provider -> UTC date of last alert

    async def emit(self, event: str, details: str) -> None:
        if self._db is None:
            return
        try:
            await self._db.log_event(event, details)
        except Exception as exc:
            logger.error("image_stats_write_failed event=%s error=%s", event, exc)

    async def alert_config_error(self, provider: str, error: str) -> None:
        if self._alerter is None:
            return
        today = datetime.now(timezone.utc).date().isoformat()
        if self._alerted_on.get(provider) == today:
            return
        try:
            await self._alerter.send_alert(
                f"Image generation {provider} provider config error ({error}); "
                "check its key and billing. The other provider keeps "
                "serving, posts degrade to text-only when both fail."
            )
        except Exception as exc:
            # the dedup key stays unset: a failed delivery must not consume
            # the daily alert, the next config error retries
            logger.error("image_alert_failed provider=%s error=%s", provider, exc)
            return
        self._alerted_on[provider] = today


class FallbackImageGenerator:
    """Tries the primary generator, falls back on any of its errors.

    The wired providers are named for the bot_stats events and config
    alerts; the defaults match the deployed fal -> yandex_art pair."""

    def __init__(
        self,
        primary: ImageGenerator,
        fallback: ImageGenerator | None = None,
        *,
        primary_name: str = "fal",
        fallback_name: str = "yandex_art",
        db: Database | None = None,
        alerter: Alerter | None = None,
    ) -> None:
        self._primary = primary
        self._fallback = fallback
        self._primary_name = primary_name
        self._fallback_name = fallback_name
        self._observer = _ChannelObserver(db, alerter)

    async def generate(self, prompt: str) -> bytes:
        try:
            data = await self._primary.generate(prompt)
        except ImageGenerationError as exc:
            return await self._via_fallback(prompt, exc)
        await self._observer.emit(
            _event_for(self._primary_name), f"provider={self._primary_name}"
        )
        return data

    async def _via_fallback(
        self, prompt: str, primary_error: ImageGenerationError
    ) -> bytes:
        logger.warning(
            "image_fallback_used kind=%s error=%s", primary_error.kind, primary_error
        )
        if primary_error.kind == "config":
            await self._observer.alert_config_error(
                self._primary_name, str(primary_error)
            )
        if self._fallback is None:
            raise
        try:
            data = await self._fallback.generate(prompt)
        except ImageGenerationError as fallback_error:
            if fallback_error.kind == "config":
                await self._observer.alert_config_error(
                    self._fallback_name, str(fallback_error)
                )
            # the fallback's kind classifies the outcome; the message keeps
            # both failures so a both-broken incident is diagnosable from
            # bot_stats alone (the llm_fallback_error_* pattern)
            raise ImageGenerationError(
                fallback_error.kind,
                _truncate(
                    f"primary={self._primary_name}: {primary_error}; "
                    f"fallback={self._fallback_name}: {fallback_error}"
                ),
            ) from fallback_error
        await self._observer.emit(
            _event_for(self._fallback_name),
            f"provider={self._fallback_name} "
            f"primary_kind={primary_error.kind} primary_error={primary_error}",
        )
        return data
