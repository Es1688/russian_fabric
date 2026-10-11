"""Two-tier weather provider chain (plan section 4).

The primary is the Open-Meteo batch provider; met.no (Yr) is the
second-queue fallback that needs no API key. The chain is a provider
itself: the scheduler slot talks to one ``WeatherProvider`` and stays
ignorant of which source actually answered.

Both error kinds are switch-worthy (see ``weather/base.py``): an infra
failure means "retrying with the fallback provider is reasonable", a
data failure means "another provider may still succeed" — including the
primary-point failure, where the fallback gets a fresh chance to fetch
the whole set before the slot is skipped with an alert.

Only ``WeatherProviderError`` switches tiers: raw programming errors
propagate immediately, as everywhere else in the transport layer.
"""

from __future__ import annotations

from max_news_bot.utils.logging import get_logger
from max_news_bot.weather.base import (
    WeatherProvider,
    WeatherProviderError,
    WeatherSnapshot,
)


logger = get_logger(__name__)


class FallbackWeatherProvider:
    """Primary provider first, the fallback when the primary fails."""

    def __init__(
        self,
        primary: WeatherProvider,
        secondary: WeatherProvider,
    ) -> None:
        self._primary = primary
        self._secondary = secondary

    async def fetch(self) -> WeatherSnapshot:
        # Kept outside the except block: the `as` name is unbound once the
        # block ends, and the final error message needs the primary cause.
        primary_error: WeatherProviderError | None = None
        try:
            return await self._primary.fetch()
        except WeatherProviderError as error:
            primary_error = error
            logger.warning(
                "weather_primary_failed provider=%s error=%s; switching to fallback",
                type(self._primary).__name__,
                error,
            )
        try:
            return await self._secondary.fetch()
        except WeatherProviderError as secondary_error:
            raise WeatherProviderError(
                f"both weather providers failed: "
                f"{type(self._primary).__name__}: {primary_error}; "
                f"{type(self._secondary).__name__}: {secondary_error}"
            ) from secondary_error
