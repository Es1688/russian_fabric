"""Weather provider interface and the unified data model (plan section 4).

The weather rubric (07:05) renders one forecast for Saint Petersburg plus the
six Leningrad Oblast cities. Providers differ — Open-Meteo fetches every point
in one batch request, the met.no fallback issues one request per point — but
they must all parse their payloads into the same ``WeatherSnapshot`` so the
rules table, the template and the validator stay provider-agnostic.

Points and their order come from ``rubrics.yaml`` (``weather.points``): the
first point is the primary one (Saint Petersburg). Failure semantics of plan
section 4: a failed regional point just drops out of the post, while a failed
primary point skips the whole slot with an alert — providers encode that by
raising instead of ever returning a snapshot without the primary point.

Sunrise/sunset are deliberately NOT part of the model: plan section 4 computes
them locally with a pure formula, without any API dependency.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from max_news_bot.config.rubrics import WeatherPoint

# Daypart structure of the post: the primary point is described in detail —
# утро, день, вечер (plan section 4). Window boundaries in local hours live
# in the provider; the model only carries the labels.
MORNING = "morning"  # 06:00–11:59 local
DAY = "day"  # 12:00–16:59 local
EVENING = "evening"  # 17:00–22:59 local
WINDOW_LABELS = (MORNING, DAY, EVENING)


class WeatherProviderError(Exception):
    """Base error for every weather provider."""


class WeatherInfraError(WeatherProviderError):
    """Transport-level failure: network, timeout, HTTP error, bad JSON.

    Maps to the "infra" kind of the factory taxonomy; retrying with the
    fallback provider (met.no) is reasonable."""


class WeatherDataError(WeatherProviderError):
    """The response arrived but is unusable: wrong shape or no data.

    Maps to the "content" kind: the payload itself is wrong, another
    provider may still succeed."""


@dataclass(frozen=True)
class WeatherWindow:
    """One daypart aggregate for one point (local time of the request).

    ``weather_code`` is the WMO weather interpretation code dominant in the
    window; the rules table (umbrella, ice, heat) reads it together with
    ``precipitation``."""

    label: str  # one of WINDOW_LABELS
    temperature: float  # mean, °C
    apparent_temperature: float  # mean "feels like", °C
    precipitation: float  # sum, mm
    wind_speed: float  # max, m/s
    weather_code: int


@dataclass(frozen=True)
class PointForecast:
    """One point's day forecast; feeds the rules table and the template."""

    point: WeatherPoint
    date: str  # 'YYYY-MM-DD' in the local timezone of the request
    windows: tuple[WeatherWindow, ...]  # subset of WINDOW_LABELS order
    temperature_min: float  # °C over the whole day (ice rule: zero crossing)
    temperature_max: float  # °C over the whole day
    precipitation_total: float  # mm over the whole day
    wind_speed_max: float  # m/s over the whole day

    def window(self, label: str) -> WeatherWindow | None:
        """The aggregate for one daypart; None when it had no usable data."""
        for entry in self.windows:
            if entry.label == label:
                return entry
        return None


@dataclass(frozen=True)
class WeatherSnapshot:
    """Parsed forecast for one request across all points, order preserved.

    ``primary`` is the first configured point (Saint Petersburg) and is
    always present; failed regional points are simply absent from
    ``regional`` so the post goes out without them (plan section 4)."""

    generated_at: datetime  # UTC moment of the successful parse
    primary: PointForecast
    regional: tuple[PointForecast, ...]

    @property
    def points(self) -> tuple[PointForecast, ...]:
        """All usable points in config order (primary first)."""
        return (self.primary, *self.regional)


@runtime_checkable
class WeatherProvider(Protocol):
    """A weather source; points are bound at construction time.

    Implementations own their transport: their own timeout, their own
    session handling and typed errors — a hanging or broken API must never
    leak raw aiohttp exceptions into the scheduler loop."""

    async def fetch(self) -> WeatherSnapshot:
        ...
