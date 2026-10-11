from __future__ import annotations

from datetime import datetime, timezone

import pytest

from max_news_bot.config.rubrics import WeatherPoint
from max_news_bot.weather.base import (
    PointForecast,
    WeatherDataError,
    WeatherInfraError,
    WeatherProvider,
    WeatherProviderError,
    WeatherSnapshot,
)
from max_news_bot.weather.fallback import FallbackWeatherProvider


_POINT = WeatherPoint(name="Санкт-Петербург", latitude=59.9386, longitude=30.3141)


def make_snapshot() -> WeatherSnapshot:
    forecast = PointForecast(
        point=_POINT,
        date="2026-10-10",
        windows=(),
        temperature_min=1.0,
        temperature_max=5.0,
        precipitation_total=0.0,
        wind_speed_max=2.0,
    )
    return WeatherSnapshot(
        generated_at=datetime.now(timezone.utc),
        primary=forecast,
        regional=(),
    )


class StubProvider:
    """Scripted provider: raises or returns its canned snapshot."""

    def __init__(
        self,
        *,
        snapshot: WeatherSnapshot | None = None,
        error: Exception | None = None,
    ) -> None:
        self.fetches = 0
        self._snapshot = snapshot
        self._error = error

    async def fetch(self) -> WeatherSnapshot:
        self.fetches += 1
        if self._error is not None:
            raise self._error
        assert self._snapshot is not None
        return self._snapshot


async def test_primary_success_never_touches_the_fallback() -> None:
    primary_snapshot = make_snapshot()
    primary = StubProvider(snapshot=primary_snapshot)
    secondary = StubProvider(snapshot=make_snapshot())

    chain = FallbackWeatherProvider(primary, secondary)
    result = await chain.fetch()

    assert result is primary_snapshot
    assert primary.fetches == 1
    assert secondary.fetches == 0


async def test_primary_infra_error_switches_to_fallback() -> None:
    primary = StubProvider(
        error=WeatherInfraError("open-meteo returned HTTP 502: upstream")
    )
    fallback_snapshot = make_snapshot()
    secondary = StubProvider(snapshot=fallback_snapshot)

    chain = FallbackWeatherProvider(primary, secondary)
    result = await chain.fetch()

    assert result is fallback_snapshot
    assert primary.fetches == 1
    assert secondary.fetches == 1


async def test_primary_data_error_switches_to_fallback() -> None:
    # a data failure also switches tiers (weather/base.py): another
    # provider may still return a usable primary point
    primary = StubProvider(
        error=WeatherDataError(
            "primary point 'Санкт-Петербург' returned no usable data"
        )
    )
    fallback_snapshot = make_snapshot()
    secondary = StubProvider(snapshot=fallback_snapshot)

    chain = FallbackWeatherProvider(primary, secondary)
    result = await chain.fetch()

    assert result is fallback_snapshot


async def test_both_failed_raises_with_both_causes() -> None:
    primary = StubProvider(error=WeatherInfraError("open-meteo timed out"))
    secondary = StubProvider(error=WeatherInfraError("met.no returned HTTP 500"))

    chain = FallbackWeatherProvider(primary, secondary)
    with pytest.raises(WeatherProviderError) as exc_info:
        await chain.fetch()

    message = str(exc_info.value)
    assert "both weather providers failed" in message
    assert "open-meteo timed out" in message
    assert "met.no returned HTTP 500" in message
    assert isinstance(exc_info.value.__cause__, WeatherInfraError)


async def test_unexpected_error_propagates_without_fallback() -> None:
    primary = StubProvider(error=RuntimeError("bug, not a provider failure"))
    secondary = StubProvider(snapshot=make_snapshot())

    chain = FallbackWeatherProvider(primary, secondary)
    with pytest.raises(RuntimeError):
        await chain.fetch()

    assert secondary.fetches == 0


def test_chain_satisfies_the_protocol() -> None:
    chain = FallbackWeatherProvider(
        StubProvider(snapshot=make_snapshot()), StubProvider()
    )

    assert isinstance(chain, WeatherProvider)
