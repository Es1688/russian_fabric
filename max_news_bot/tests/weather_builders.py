"""Builders for weather model fixtures shared by the weather tests.

The provider contract lives in ``max_news_bot/weather/base.py``; these
helpers assemble snapshots by hand — no HTTP, no real coordinates
beyond the channel's own points.
"""

from __future__ import annotations

from datetime import datetime, timezone

from max_news_bot.config.rubrics import WeatherPoint
from max_news_bot.weather.base import (
    DAY,
    EVENING,
    MORNING,
    PointForecast,
    WeatherSnapshot,
    WeatherWindow,
)

SPB = WeatherPoint(name="Санкт-Петербург", latitude=59.9386, longitude=30.3141)
GATCHINA = WeatherPoint(name="Гатчина", latitude=59.5680, longitude=30.1285)
VYBORG = WeatherPoint(name="Выборг", latitude=60.7104, longitude=28.7519)
TIKHVIN = WeatherPoint(name="Тихвин", latitude=59.6455, longitude=33.5222)

# A calm north-western October day: the neutral baseline every rule test
# tweaks. No precipitation, no wind, no swings — only the seasonal tip.
NEUTRAL_DAY = "2026-10-10"


def window(
    label: str = DAY,
    temperature: float = 6.0,
    apparent_temperature: float | None = None,
    precipitation: float = 0.0,
    wind_speed: float = 4.0,
    weather_code: int = 1,
) -> WeatherWindow:
    return WeatherWindow(
        label=label,
        temperature=temperature,
        apparent_temperature=(
            temperature if apparent_temperature is None else apparent_temperature
        ),
        precipitation=precipitation,
        wind_speed=wind_speed,
        weather_code=weather_code,
    )


def point(
    base: WeatherPoint = SPB,
    *,
    date: str = NEUTRAL_DAY,
    windows: tuple[WeatherWindow, ...] | None = None,
    temperature_min: float | None = None,
    temperature_max: float | None = None,
    precipitation_total: float | None = None,
    wind_speed_max: float | None = None,
) -> PointForecast:
    if windows is None:
        windows = (
            window(MORNING, temperature=3.0),
            window(DAY, temperature=6.0),
            window(EVENING, temperature=4.0),
        )
    temps = [entry.temperature for entry in windows]
    precip = sum(entry.precipitation for entry in windows)
    winds = [entry.wind_speed for entry in windows]
    return PointForecast(
        point=base,
        date=date,
        windows=windows,
        temperature_min=min(temps) if temperature_min is None else temperature_min,
        temperature_max=max(temps) if temperature_max is None else temperature_max,
        precipitation_total=(
            round(precip, 1) if precipitation_total is None else precipitation_total
        ),
        wind_speed_max=(
            round(max(winds), 1) if wind_speed_max is None else wind_speed_max
        ),
    )


def snapshot(
    primary: PointForecast | None = None,
    regional: tuple[PointForecast, ...] = (),
    *,
    generated_at: datetime | None = None,
) -> WeatherSnapshot:
    return WeatherSnapshot(
        generated_at=generated_at or datetime(2026, 10, 10, 4, 5, tzinfo=timezone.utc),
        primary=primary if primary is not None else point(),
        regional=regional,
    )


def full_snapshot(
    primary: PointForecast | None = None,
    regional: tuple[PointForecast, ...] | None = None,
) -> WeatherSnapshot:
    """SPb plus three region towns — the realistic post shape."""
    return snapshot(
        primary=primary if primary is not None else point(),
        regional=(
            regional
            if regional is not None
            else (
                point(GATCHINA),
                point(VYBORG),
                point(TIKHVIN),
            )
        ),
    )
