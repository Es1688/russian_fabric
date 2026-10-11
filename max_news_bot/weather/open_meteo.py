"""Open-Meteo provider: one batch HTTP request for all points (plan section 4).

All seven coordinates travel in a single GET — comma-joined latitude and
longitude lists — so one forecast is one HTTP transaction instead of seven
(plan section 4: "одна HTTP-транзакция вместо семи").

The transport is isolated the same way as the rest of the project (see
``max_api/client.py`` and ``parsers/rss.py``): its own ``ClientTimeout``, a
short-lived session per call unless a shared one is injected, and every
failure converted to a typed ``WeatherInfraError``/``WeatherDataError``.

Sunrise/sunset are NOT requested: plan section 4 computes them locally with
a pure formula, the API stays out of it.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from typing import Any

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
    WeatherSnapshot,
    WeatherWindow,
)


logger = get_logger(__name__)

_DEFAULT_BASE_URL = "https://api.open-meteo.com/v1/forecast"
# The batch of 7 points is one small GET; the scheduler slot has its own
# grace budget, so the call must fail fast rather than hang until it.
_DEFAULT_TIMEOUT = 15
_DEFAULT_TIMEZONE = "Europe/Moscow"
_ERROR_BODY_LEN = 200

_HOURLY_VARS = (
    "temperature_2m",
    "apparent_temperature",
    "precipitation",
    "weather_code",
    "wind_speed_10m",
)

# Daypart windows in local hours [start, end] inclusive — утро, день, вечер
# of the primary point (plan section 4).
_WINDOWS = (
    (MORNING, 6, 11),
    (DAY, 12, 16),
    (EVENING, 17, 22),
)


class OpenMeteoProvider:
    """Fetches every configured point with a single Open-Meteo request."""

    def __init__(
        self,
        points: tuple[WeatherPoint, ...] | list[WeatherPoint],
        *,
        session: aiohttp.ClientSession | None = None,
        timeout: int = _DEFAULT_TIMEOUT,
        base_url: str = _DEFAULT_BASE_URL,
        timezone_name: str = _DEFAULT_TIMEZONE,
    ) -> None:
        if not points:
            raise ValueError("OpenMeteoProvider requires at least one point")
        self._points = tuple(points)
        # Shared process-wide session wins; otherwise one per call.
        self._session = session
        self._timeout = timeout
        self._base_url = base_url.rstrip("/")
        self._timezone_name = timezone_name

    async def fetch(self) -> WeatherSnapshot:
        """One HTTP transaction for all points, parsed into the model.

        Raises WeatherInfraError on transport failures and WeatherDataError
        when the payload is unusable; a missing primary point is a data
        error (the slot must be skipped, plan section 4)."""
        logger.debug("open_meteo_request points=%d", len(self._points))
        payload = await self._get_json(self._build_params())
        entries = self._entries(payload)
        forecasts = [
            _parse_entry(entry, point)
            for entry, point in zip(entries, self._points, strict=True)
        ]
        return self._assemble(forecasts)

    def _build_params(self) -> dict[str, str]:
        # Comma-joined coordinate lists are the documented batch form;
        # passing lists to aiohttp would emit repeated keys instead, which
        # the API does not accept.
        return {
            "latitude": ",".join(str(point.latitude) for point in self._points),
            "longitude": ",".join(str(point.longitude) for point in self._points),
            "hourly": ",".join(_HOURLY_VARS),
            "timezone": self._timezone_name,
            "forecast_days": "1",
            "wind_speed_unit": "ms",  # the post speaks м/с; API default is km/h
        }

    async def _get_json(self, params: dict[str, str]) -> Any:
        """One isolated GET; every failure becomes a typed infra error."""
        client_timeout = aiohttp.ClientTimeout(total=self._timeout)
        try:
            if self._session is not None:
                status, body = await _send(self._session, self._base_url, params, client_timeout)
            else:
                async with aiohttp.ClientSession() as session:
                    status, body = await _send(session, self._base_url, params, client_timeout)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise WeatherInfraError(f"open-meteo request failed: {exc}") from exc
        if not 200 <= status < 300:
            reason = _error_reason(body)
            detail = reason if reason else body[:_ERROR_BODY_LEN]
            raise WeatherInfraError(f"open-meteo returned HTTP {status}: {detail}")
        try:
            return json.loads(body)
        except ValueError as exc:
            raise WeatherInfraError(f"open-meteo response is not JSON: {exc}") from exc

    def _entries(self, payload: Any) -> list[Any]:
        """Normalize the batch reply to one entry per requested point.

        A multi-coordinate reply is a JSON array whose order matches the
        request coordinate order (documented API behavior — the echoed
        latitude/longitude are grid-snapped and must not be matched against
        the input); a single-coordinate reply is a bare object."""
        if isinstance(payload, dict):
            entries: list[Any] = [payload] if len(self._points) == 1 else []
            if not entries:
                raise WeatherDataError(
                    "open-meteo returned a single location object "
                    f"for {len(self._points)} requested points"
                )
        elif isinstance(payload, list):
            entries = payload
        else:
            raise WeatherDataError(
                f"open-meteo response is neither an object nor an array: {type(payload).__name__}"
            )
        if len(entries) != len(self._points):
            raise WeatherDataError(
                f"open-meteo returned {len(entries)} locations "
                f"for {len(self._points)} requested points"
            )
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                raise WeatherDataError(
                    f"open-meteo location[{index}] is not an object"
                )
        return entries

    def _assemble(self, forecasts: list[PointForecast | None]) -> WeatherSnapshot:
        """Apply the failure semantics of plan section 4 to parsed points."""
        primary = forecasts[0]
        if primary is None:
            raise WeatherDataError(
                f"primary point {self._points[0].name!r} returned no usable "
                "data; the weather slot must be skipped with an alert "
                "(plan section 4)"
            )
        regional: list[PointForecast] = []
        for point, forecast in zip(self._points[1:], forecasts[1:], strict=True):
            if forecast is None:
                # A failed region city drops out of the post, the rest go out.
                logger.warning("weather_point_dropped name=%s", point.name)
                continue
            regional.append(forecast)
        return WeatherSnapshot(
            generated_at=datetime.now(timezone.utc),
            primary=primary,
            regional=tuple(regional),
        )


async def _send(
    session: aiohttp.ClientSession,
    url: str,
    params: dict[str, str],
    timeout: aiohttp.ClientTimeout,
) -> tuple[int, str]:
    async with session.get(url, params=params, timeout=timeout) as response:
        return response.status, await response.text()


def _parse_entry(entry: dict[str, Any], point: WeatherPoint) -> PointForecast | None:
    """Parse one location object; None when the point has no usable data."""
    hourly = entry.get("hourly")
    if not isinstance(hourly, dict):
        raise WeatherDataError(f"point {point.name!r}: 'hourly' section is missing")

    times = _require_series(hourly, "time", point)
    temps = [_as_number(v) for v in _require_series(hourly, "temperature_2m", point)]
    apparents = [
        _as_number(v) for v in _require_series(hourly, "apparent_temperature", point)
    ]
    precips = [_as_number(v) for v in _require_series(hourly, "precipitation", point)]
    winds = [_as_number(v) for v in _require_series(hourly, "wind_speed_10m", point)]
    codes = [_as_code(v) for v in _require_series(hourly, "weather_code", point)]

    if not times:
        raise WeatherDataError(f"point {point.name!r}: hourly.time is empty")
    hours = _local_hours(times, point)
    for name, values in (
        ("temperature_2m", temps),
        ("apparent_temperature", apparents),
        ("precipitation", precips),
        ("wind_speed_10m", winds),
        ("weather_code", codes),
    ):
        if len(values) != len(hours):
            raise WeatherDataError(
                f"point {point.name!r}: hourly.{name} has {len(values)} values, "
                f"expected {len(hours)}"
            )

    valid_temps = [value for value in temps if value is not None]
    if not valid_temps:
        # "Упавшая точка": null-filled series -> unusable, dropped upstream.
        return None

    windows = tuple(
        window
        for window in (
            _build_window(label, start, end, hours, temps, apparents, precips, winds, codes)
            for label, start, end in _WINDOWS
        )
        if window is not None
    )
    valid_precip = [value for value in precips if value is not None]
    valid_wind = [value for value in winds if value is not None]
    return PointForecast(
        point=point,
        date=times[0][:10],
        windows=windows,
        temperature_min=round(min(valid_temps), 1),
        temperature_max=round(max(valid_temps), 1),
        precipitation_total=round(sum(valid_precip), 1),
        wind_speed_max=round(max(valid_wind), 1) if valid_wind else 0.0,
    )


def _build_window(
    label: str,
    start_hour: int,
    end_hour: int,
    hours: list[int],
    temps: list[float | None],
    apparents: list[float | None],
    precips: list[float | None],
    winds: list[float | None],
    codes: list[int | None],
) -> WeatherWindow | None:
    """Aggregate one daypart; None when it has no usable temperatures."""
    indexes = [i for i, hour in enumerate(hours) if start_hour <= hour <= end_hour]
    window_temps = [temps[i] for i in indexes if temps[i] is not None]
    if not window_temps:
        return None
    window_apparent = [apparents[i] for i in indexes if apparents[i] is not None]
    window_precip = [precips[i] for i in indexes if precips[i] is not None]
    window_wind = [winds[i] for i in indexes if winds[i] is not None]
    window_codes = [codes[i] for i in indexes if codes[i] is not None]
    return WeatherWindow(
        label=label,
        temperature=_mean(window_temps),
        apparent_temperature=_mean(window_apparent if window_apparent else window_temps),
        precipitation=round(sum(window_precip), 1),
        wind_speed=round(max(window_wind), 1) if window_wind else 0.0,
        weather_code=_dominant_code(window_codes),
    )


def _require_series(hourly: dict[str, Any], name: str, point: WeatherPoint) -> list[Any]:
    values = hourly.get(name)
    if not isinstance(values, list):
        raise WeatherDataError(f"point {point.name!r}: hourly.{name} is missing or not a list")
    return values


def _as_number(value: Any) -> float | None:
    # bool is an int subclass: a stray true/false must not count as 1.0/0.0
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _as_code(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _local_hours(times: list[Any], point: WeatherPoint) -> list[int]:
    """Hours from ISO-like local stamps 'YYYY-MM-DDTHH:MM'."""
    hours: list[int] = []
    for stamp in times:
        if not isinstance(stamp, str) or len(stamp) < 13 or stamp[10] != "T":
            raise WeatherDataError(
                f"point {point.name!r}: bad hourly time stamp {stamp!r}"
            )
        try:
            hours.append(int(stamp[11:13]))
        except ValueError as exc:
            raise WeatherDataError(
                f"point {point.name!r}: bad hourly time stamp {stamp!r}"
            ) from exc
    return hours


def _mean(values: list[float]) -> float:
    return round(sum(values) / len(values), 1)


def _dominant_code(codes: list[int]) -> int:
    """Most frequent WMO code; ties keep the earliest-seen code. 0 if empty."""
    counts: dict[int, int] = {}
    best: int | None = None
    for code in codes:
        counts[code] = counts.get(code, 0) + 1
        if best is None or counts[code] > counts[best]:
            best = code
    return best if best is not None else 0


def _error_reason(body: str) -> str | None:
    """Open-Meteo errors are JSON with a human-readable 'reason'."""
    try:
        data = json.loads(body)
    except ValueError:
        return None
    if isinstance(data, dict):
        reason = data.get("reason")
        if isinstance(reason, str) and reason.strip():
            return reason.strip()
    return None
