"""met.no (Yr) provider: the second-tier weather fallback (plan section 4).

met.no needs no API key but carries no batch form, so the fallback issues
one request per point — seven separate HTTP transactions, each with its
own timeout, run concurrently and isolated from each other: a hanging or
broken point must not affect the rest (plan section 4: "семь отдельных
запросов со своими таймаутами и изоляцией").

The Yr terms of service require a uniquely identifying User-Agent, so one
is always sent; ``base_url`` and ``user_agent`` are injectable for tests.

Differences against Open-Meteo handled here:
- timeseries stamps are UTC; daypart windows are local (Europe/Moscow),
  so every stamp is converted and only the local day of the first stamp
  is aggregated (the slot posts today's forecast, not tomorrow's tail);
- symbol codes ("rain", "lightrainshowers_day"...) are mapped onto the
  WMO weather interpretation codes the rules table reads;
- there is no "feels like" field: apparent temperature is computed with
  the pure Australian BOM formula (temperature, humidity, wind), the same
  no-API approach plan section 4 prescribes for sunrise/sunset.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiohttp

from max_news_bot.config.rubrics import WeatherPoint
from max_news_bot.utils.logging import get_logger
from max_news_bot.weather.base import (
    DAY,
    EVENING,
    MORNING,
    PointForecast,
    WeatherDataError,
    WeatherInfraError,
    WeatherProviderError,
    WeatherSnapshot,
    WeatherWindow,
)


logger = get_logger(__name__)

_DEFAULT_BASE_URL = "https://api.met.no/weatherapi/locationforecast/2.0/compact"
# No batch means seven sequential-or-parallel requests inside the slot's
# grace budget; each point gets its own short budget, a slow one must not
# stretch the whole fetch.
_DEFAULT_TIMEOUT = 10
_DEFAULT_TIMEZONE = "Europe/Moscow"
# Yr conditions of use: a unique, contactable User-Agent is mandatory.
_DEFAULT_USER_AGENT = "content-fabric-max-bot/0.1 (https://github.com/Es1688/content_fabric_max)"
_ERROR_BODY_LEN = 200
# met.no asks for coordinates rounded to at most four decimals.
_COORD_PRECISION = 4

# Daypart windows in local hours [start, end] inclusive — утро, день, вечер
# of the primary point (plan section 4); same boundaries as Open-Meteo.
_WINDOWS = (
    (MORNING, 6, 11),
    (DAY, 12, 16),
    (EVENING, 17, 22),
)

# Yr symbol code -> WMO weather interpretation code (the shared truth the
# rules table reads). Yr has no intensity split inside showers families,
# so showers map onto the moderate WMO codes. "_day"/"_night"/
# "_polartwilight" variants are stripped before the lookup; an unknown
# symbol yields None and simply drops out of the window aggregation.
_SYMBOL_TO_WMO: dict[str, int] = {
    "clearsky": 0,
    "fair": 1,
    "partlycloudy": 2,
    "cloudy": 3,
    "fog": 45,
    "lightrain": 61,
    "rain": 63,
    "heavyrain": 65,
    "lightrainshowers": 80,
    "rainshowers": 81,
    "heavyrainshowers": 82,
    "lightsleet": 66,
    "sleet": 66,
    "heavysleet": 67,
    "lightsleetshowers": 66,
    "sleetshowers": 66,
    "heavysleetshowers": 67,
    "lightsnow": 71,
    "snow": 73,
    "heavysnow": 75,
    "lightsnowshowers": 85,
    "snowshowers": 85,
    "heavysnowshowers": 86,
    # every thunderstorm variant: the rules table only needs "storm"
    "lightrainandthunder": 95,
    "rainandthunder": 95,
    "heavyrainandthunder": 95,
    "lightsnowandthunder": 95,
    "snowandthunder": 95,
    "heavysnowandthunder": 95,
    "lightsleetandthunder": 95,
    "sleetandthunder": 95,
    "heavysleetandthunder": 95,
    "lightrainshowersandthunder": 95,
    "rainshowersandthunder": 95,
    "heavyrainshowersandthunder": 95,
    "lightsnowshowersandthunder": 95,
    "snowshowersandthunder": 95,
    "heavysnowshowersandthunder": 95,
    "lightsleetshowersandthunder": 95,
    "sleetshowersandthunder": 95,
    "heavysleetshowersandthunder": 95,
    # Yr keeps two historical double-s spellings of these identifiers
    "lightssnowshowersandthunder": 95,
    "lightssleetshowersandthunder": 95,
}
_SYMBOL_SUFFIXES = ("_polartwilight", "_night", "_day")


@dataclass(frozen=True)
class _Hour:
    """One timeseries step reduced to what the model needs, local time."""

    moment: datetime  # tz-aware, in the request timezone
    temperature: float | None  # air_temperature, °C
    apparent: float | None  # computed "feels like", °C
    precipitation: float | None  # next_1_hours amount, mm
    wind_speed: float | None  # m/s (the API default unit)
    weather_code: int | None  # WMO code from the symbol map


class MetNoProvider:
    """Fetches every configured point with its own single met.no request."""

    def __init__(
        self,
        points: tuple[WeatherPoint, ...] | list[WeatherPoint],
        *,
        session: aiohttp.ClientSession | None = None,
        timeout: int = _DEFAULT_TIMEOUT,
        base_url: str = _DEFAULT_BASE_URL,
        timezone_name: str = _DEFAULT_TIMEZONE,
        user_agent: str = _DEFAULT_USER_AGENT,
    ) -> None:
        if not points:
            raise ValueError("MetNoProvider requires at least one point")
        self._points = tuple(points)
        # Shared process-wide session wins; otherwise one per fetch() call
        # is reused by all per-point requests (connections pool, timeouts
        # stay per request).
        self._session = session
        self._timeout = timeout
        self._base_url = base_url.rstrip("/")
        try:
            self._tzinfo = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown timezone {timezone_name!r}") from exc
        self._user_agent = user_agent

    async def fetch(self) -> WeatherSnapshot:
        """Seven isolated per-point requests, parsed into the model.

        Raises WeatherInfraError on transport failures and WeatherDataError
        when a payload is unusable; a failed primary point fails the whole
        attempt, while a failed regional point just drops out of the post
        (plan section 4)."""
        logger.debug("met_no_fetch_start points=%d", len(self._points))
        if self._session is not None:
            forecasts = await self._fetch_all(self._session)
        else:
            async with aiohttp.ClientSession() as session:
                forecasts = await self._fetch_all(session)
        return self._assemble(forecasts)

    async def _fetch_all(
        self, session: aiohttp.ClientSession
    ) -> list[PointForecast | None]:
        """One request per point, each with its own timeout and isolation.

        ``gather(return_exceptions=True)`` waits for every point: a slow or
        hanging point burns only its own budget and never cancels the rest."""
        tasks = [self._fetch_point(session, point) for point in self._points]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        forecasts: list[PointForecast | None] = []
        for index, result in enumerate(results):
            if isinstance(result, BaseException):
                if index == 0:
                    # The primary point failure fails the whole attempt
                    # with its original typed error: the infra/data split
                    # must survive into the fallback chain and the alert.
                    if isinstance(result, WeatherProviderError):
                        raise result
                    raise WeatherInfraError(
                        f"met.no primary point request crashed: {result}"
                    ) from result
                # A regional point failure degrades the post, not the slot.
                forecasts.append(None)
                logger.warning(
                    "met_no_point_failed name=%s error=%s",
                    self._points[index].name,
                    result,
                )
            else:
                forecasts.append(result)
        return forecasts

    async def _fetch_point(
        self, session: aiohttp.ClientSession, point: WeatherPoint
    ) -> PointForecast | None:
        payload = await self._get_json(session, point)
        return _parse_payload(payload, point, self._tzinfo)

    async def _get_json(
        self, session: aiohttp.ClientSession, point: WeatherPoint
    ) -> Any:
        """One isolated GET with its own timeout; typed errors on failure."""
        params = {
            "lat": f"{point.latitude:.{_COORD_PRECISION}f}",
            "lon": f"{point.longitude:.{_COORD_PRECISION}f}",
        }
        client_timeout = aiohttp.ClientTimeout(total=self._timeout)
        try:
            async with session.get(
                self._base_url,
                params=params,
                timeout=client_timeout,
                headers={"User-Agent": self._user_agent},
            ) as response:
                status, body = response.status, await response.text()
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise WeatherInfraError(
                f"met.no request for {point.name!r} failed: {exc}"
            ) from exc
        if not 200 <= status < 300:
            detail = body.strip()[:_ERROR_BODY_LEN]
            raise WeatherInfraError(
                f"met.no returned HTTP {status} for {point.name!r}: {detail}"
            )
        try:
            return json.loads(body)
        except ValueError as exc:
            raise WeatherInfraError(
                f"met.no response for {point.name!r} is not JSON: {exc}"
            ) from exc

    def _assemble(self, forecasts: list[PointForecast | None]) -> WeatherSnapshot:
        """Apply the failure semantics of plan section 4 to parsed points."""
        primary = forecasts[0]
        if primary is None:
            raise WeatherDataError(
                f"primary point {self._points[0].name!r} returned no usable "
                "met.no data; the weather slot must be skipped with an alert "
                "(plan section 4)"
            )
        regional = tuple(
            forecast
            for forecast in forecasts[1:]
            if forecast is not None
        )
        return WeatherSnapshot(
            generated_at=datetime.now(timezone.utc),
            primary=primary,
            regional=regional,
        )


def _parse_payload(
    payload: Any, point: WeatherPoint, tzinfo: ZoneInfo
) -> PointForecast | None:
    """Parse one locationforecast reply; None when the point has no data."""
    properties = payload.get("properties") if isinstance(payload, dict) else None
    if not isinstance(properties, dict):
        raise WeatherDataError(
            f"point {point.name!r}: met.no payload has no 'properties' object"
        )
    timeseries = properties.get("timeseries")
    if not isinstance(timeseries, list):
        raise WeatherDataError(
            f"point {point.name!r}: met.no 'properties.timeseries' is missing"
        )
    if not timeseries:
        # An empty series is an unusable point: it drops out upstream.
        return None

    hours = [_parse_step(entry, point, tzinfo) for entry in timeseries]
    # Today's local day of the first stamp; the API starts at the current
    # hour, so later days are tomorrow's tail and stay out of the post.
    date = hours[0].moment.strftime("%Y-%m-%d")
    day = [hour for hour in hours if hour.moment.strftime("%Y-%m-%d") == date]

    day_temps = [h.temperature for h in day if h.temperature is not None]
    if not day_temps:
        # "Упавшая точка": null-filled series -> unusable, dropped upstream.
        return None

    windows = tuple(
        window
        for window in (
            _build_window(label, start, end, day) for label, start, end in _WINDOWS
        )
        if window is not None
    )
    day_precip = [h.precipitation for h in day if h.precipitation is not None]
    day_wind = [h.wind_speed for h in day if h.wind_speed is not None]
    return PointForecast(
        point=point,
        date=date,
        windows=windows,
        temperature_min=round(min(day_temps), 1),
        temperature_max=round(max(day_temps), 1),
        precipitation_total=round(sum(day_precip), 1),
        wind_speed_max=round(max(day_wind), 1) if day_wind else 0.0,
    )


def _parse_step(entry: Any, point: WeatherPoint, tzinfo: ZoneInfo) -> _Hour:
    """Flatten one timeseries step (instant + next_1_hours) into an _Hour."""
    if not isinstance(entry, dict):
        raise WeatherDataError(
            f"point {point.name!r}: met.no timeseries step is not an object"
        )
    moment = _parse_utc(entry.get("time"), point).astimezone(tzinfo)
    data = entry.get("data")
    if not isinstance(data, dict):
        raise WeatherDataError(
            f"point {point.name!r}: met.no timeseries step has no 'data'"
        )
    details = data.get("instant")
    details = details.get("details") if isinstance(details, dict) else None
    if not isinstance(details, dict):
        raise WeatherDataError(
            f"point {point.name!r}: met.no timeseries step has no instant details"
        )
    temperature = _as_number(details.get("air_temperature"))
    wind = _as_number(details.get("wind_speed"))
    humidity = _as_number(details.get("relative_humidity"))
    symbol, precipitation = _next_1_hours(data)
    return _Hour(
        moment=moment,
        temperature=temperature,
        apparent=_apparent_temperature(temperature, humidity, wind),
        precipitation=precipitation,
        wind_speed=wind,
        weather_code=_symbol_to_wmo(symbol),
    )


def _next_1_hours(data: dict[str, Any]) -> tuple[str | None, float | None]:
    """The hourly symbol code and precipitation amount, when present.

    Steps without ``next_1_hours`` (the API trims them at the range edges)
    contribute no precipitation and no weather code."""
    block = data.get("next_1_hours")
    if not isinstance(block, dict):
        return None, None
    summary = block.get("summary")
    symbol = summary.get("symbol_code") if isinstance(summary, dict) else None
    if not isinstance(symbol, str):
        symbol = None
    details = block.get("details")
    amount = details.get("precipitation_amount") if isinstance(details, dict) else None
    return symbol, _as_number(amount)


def _symbol_to_wmo(symbol: str | None) -> int | None:
    if symbol is None:
        return None
    for suffix in _SYMBOL_SUFFIXES:
        if symbol.endswith(suffix):
            symbol = symbol[: -len(suffix)]
            break
    return _SYMBOL_TO_WMO.get(symbol)


def _parse_utc(raw: Any, point: WeatherPoint) -> datetime:
    """Parse an ISO-8601 UTC stamp like '2026-10-10T06:00:00Z'."""
    if not isinstance(raw, str):
        raise WeatherDataError(
            f"point {point.name!r}: bad met.no time stamp {raw!r}"
        )
    try:
        moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise WeatherDataError(
            f"point {point.name!r}: bad met.no time stamp {raw!r}"
        ) from exc
    if moment.tzinfo is None:
        raise WeatherDataError(
            f"point {point.name!r}: met.no time stamp {raw!r} has no timezone"
        )
    return moment


def _apparent_temperature(
    temperature: float | None,
    humidity: float | None,
    wind: float | None,
) -> float | None:
    """Australian BOM apparent temperature: a pure local formula.

    AT = Ta + 0.33*e - 0.70*ws - 4.00, with the water-vapour pressure
    e = rh/100 * 6.105 * exp(17.27*Ta / (237.7 + Ta)) in hPa and ws in m/s.
    Falls back to the plain temperature when inputs are missing."""
    if temperature is None:
        return None
    if humidity is None or wind is None:
        return temperature
    vapour = (
        humidity
        / 100.0
        * 6.105
        * (2.718281828459045 ** (17.27 * temperature / (237.7 + temperature)))
    )
    return temperature + 0.33 * vapour - 0.70 * wind - 4.00


def _build_window(
    label: str, start_hour: int, end_hour: int, day: list[_Hour]
) -> WeatherWindow | None:
    """Aggregate one daypart; None when it has no usable temperatures."""
    indexes = [
        i for i, hour in enumerate(day) if start_hour <= hour.moment.hour <= end_hour
    ]
    temps = [day[i].temperature for i in indexes if day[i].temperature is not None]
    if not temps:
        return None
    apparent = [
        day[i].apparent
        for i in indexes
        if day[i].apparent is not None
    ]
    precip = [day[i].precipitation for i in indexes if day[i].precipitation is not None]
    wind = [day[i].wind_speed for i in indexes if day[i].wind_speed is not None]
    codes = [day[i].weather_code for i in indexes if day[i].weather_code is not None]
    return WeatherWindow(
        label=label,
        temperature=round(sum(temps) / len(temps), 1),
        apparent_temperature=round(
            sum(apparent if apparent else temps) / len(apparent if apparent else temps),
            1,
        ),
        precipitation=round(sum(precip), 1),
        wind_speed=round(max(wind), 1) if wind else 0.0,
        weather_code=_dominant_code(codes),
    )


def _as_number(value: Any) -> float | None:
    # bool is an int subclass: a stray true/false must not count as 1.0/0.0
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _dominant_code(codes: list[int]) -> int:
    """Most frequent WMO code; ties keep the earliest-seen code. 0 if empty."""
    counts: dict[int, int] = {}
    best: int | None = None
    for code in codes:
        counts[code] = counts.get(code, 0) + 1
        if best is None or counts[code] > counts[best]:
            best = code
    return best if best is not None else 0
