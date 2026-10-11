from __future__ import annotations

import asyncio
from datetime import timezone
import json
from typing import Any
from unittest.mock import patch

import aiohttp
import pytest
from aiohttp import web

from max_news_bot.config.rubrics import WeatherPoint
from max_news_bot.weather.base import (
    DAY,
    EVENING,
    MORNING,
    WeatherDataError,
    WeatherInfraError,
    WeatherProvider,
    WeatherProviderError,
    WeatherSnapshot,
)
from max_news_bot.weather.open_meteo import OpenMeteoProvider


# The seven points of plan section 4, mirroring rubrics.yaml weather.points:
# Saint Petersburg first (primary), then the six Leningrad Oblast cities.
_POINTS = (
    WeatherPoint(name="Санкт-Петербург", latitude=59.9386, longitude=30.3141),
    WeatherPoint(name="Гатчина", latitude=59.568, longitude=30.1285),
    WeatherPoint(name="Выборг", latitude=60.7104, longitude=28.7519),
    WeatherPoint(name="Тихвин", latitude=59.6455, longitude=33.5222),
    WeatherPoint(name="Луга", latitude=58.7372, longitude=29.8458),
    WeatherPoint(name="Кингисепп", latitude=59.3733, longitude=28.6136),
    WeatherPoint(name="Кировск (Лен. обл.)", latitude=59.8833, longitude=30.9833),
)

_DATE = "2026-10-10"


class FakeOpenMeteoServer:
    """Local aiohttp server impersonating api.open-meteo.com."""

    def __init__(self) -> None:
        self.requests: list[dict[str, str]] = []
        self.payload: Any = []
        self.status: int = 200
        # when set, returned verbatim instead of json_response(payload)
        self.body: str | None = None
        self._runner: web.AppRunner | None = None

    async def start(self) -> str:
        async def forecast(request: web.Request) -> web.Response:
            self.requests.append(dict(request.query))
            if self.body is not None:
                return web.Response(
                    status=self.status, text=self.body, content_type="application/json"
                )
            return web.json_response(self.payload, status=self.status)

        app = web.Application()
        app.router.add_get("/v1/forecast", forecast)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        assert runner.addresses, "test server did not bind"
        port = runner.addresses[0][1]
        self._runner = runner
        return f"http://127.0.0.1:{port}/v1/forecast"

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()


@pytest.fixture
async def ometeo_server():
    server = FakeOpenMeteoServer()
    base_url = await server.start()
    yield server, base_url
    await server.stop()


def make_entry(
    *,
    temps: list[float | None] | None = None,
    apparent: list[float | None] | None = None,
    precip: list[float | None] | None = None,
    wind: list[float | None] | None = None,
    code: list[int | None] | None = None,
    date: str = _DATE,
) -> dict[str, Any]:
    """A 24-hour Open-Meteo location object; unset series get flat defaults."""
    series_temps: list[float | None] = temps if temps is not None else [5.0] * 24
    series_apparent: list[float | None] = (
        apparent if apparent is not None else list(series_temps)
    )
    series_precip: list[float | None] = precip if precip is not None else [0.0] * 24
    series_wind: list[float | None] = wind if wind is not None else [2.0] * 24
    series_code: list[int | None] = code if code is not None else [3] * 24
    # The API echoes grid-snapped coordinates, not the exact input values.
    return {
        "latitude": 59.9,
        "longitude": 30.3,
        "hourly": {
            "time": [f"{date}T{hour:02d}:00" for hour in range(24)],
            "temperature_2m": series_temps,
            "apparent_temperature": series_apparent,
            "precipitation": series_precip,
            "weather_code": series_code,
            "wind_speed_10m": series_wind,
        },
    }


def make_provider(base_url: str, points=..., **overrides: Any) -> OpenMeteoProvider:
    params: dict[str, Any] = {
        "points": _POINTS if points is ... else points,
        "base_url": base_url,
        "session": None,
        "timeout": 5,
    }
    params.update(overrides)
    return OpenMeteoProvider(**params)


async def test_all_points_travel_in_one_batch_request(ometeo_server) -> None:
    server, base_url = ometeo_server
    server.payload = [make_entry() for _ in _POINTS]

    snapshot = await make_provider(base_url).fetch()

    # one HTTP transaction for all 7 points (plan section 4)
    assert len(server.requests) == 1
    query = server.requests[0]
    assert query["latitude"] == ",".join(str(p.latitude) for p in _POINTS)
    assert query["longitude"] == ",".join(str(p.longitude) for p in _POINTS)
    assert query["timezone"] == "Europe/Moscow"
    assert query["forecast_days"] == "1"
    assert query["wind_speed_unit"] == "ms"
    assert query["hourly"] == (
        "temperature_2m,apparent_temperature,precipitation,weather_code,wind_speed_10m"
    )
    # the reply order maps back onto the config order: SPb primary, 6 cities
    assert isinstance(snapshot, WeatherSnapshot)
    assert snapshot.primary.point.name == "Санкт-Петербург"
    assert [f.point.name for f in snapshot.regional] == [p.name for p in _POINTS[1:]]
    assert snapshot.generated_at.tzinfo is timezone.utc


async def test_single_point_object_response(ometeo_server) -> None:
    server, base_url = ometeo_server
    server.payload = make_entry()  # bare object, not wrapped in a list

    snapshot = await make_provider(base_url, points=(_POINTS[0],)).fetch()

    assert snapshot.primary.point.name == "Санкт-Петербург"
    assert snapshot.regional == ()
    assert snapshot.points == (snapshot.primary,)


async def test_windows_and_day_aggregates_parsed(ometeo_server) -> None:
    server, base_url = ometeo_server
    temps = (
        [-5.0, -4.0, -3.0, -2.0, -1.0, -2.0]  # night 00-05
        + [-1.0, 0.0, 1.0, 2.0, 3.0, 4.0]  # morning 06-11, mean 1.5
        + [5.0, 6.0, 7.0, 8.0, 9.0]  # day 12-16, mean 7.0
        + [6.0, 5.0, 4.0, 3.0, 2.0, 1.0]  # evening 17-22, mean 3.5
        + [-3.0]  # hour 23
    )
    apparent = [value - 3.0 for value in temps]
    precip = [0.0] * 24
    precip[13] = 1.2
    precip[14] = 0.8
    precip[23] = 0.5
    wind = [2.0] * 24
    wind[18] = 9.7
    code = [3] * 24
    code[6] = 61  # morning: one rainy hour, overcast dominates
    code[12] = 3
    code[13] = 61
    code[14] = 80
    code[15] = 61
    code[16] = 61  # day: rain dominates
    code[17:23] = [0] * 6  # evening: clear
    server.payload = make_entry(
        temps=temps, apparent=apparent, precip=precip, wind=wind, code=code
    )

    snapshot = await make_provider(base_url, points=(_POINTS[0],)).fetch()
    forecast = snapshot.primary

    assert forecast.date == _DATE
    morning = forecast.window(MORNING)
    assert morning is not None
    assert morning.temperature == pytest.approx(1.5)
    assert morning.apparent_temperature == pytest.approx(-1.5)
    assert morning.precipitation == 0.0
    assert morning.weather_code == 3
    day = forecast.window(DAY)
    assert day is not None
    assert day.temperature == pytest.approx(7.0)
    assert day.precipitation == pytest.approx(2.0)
    assert day.weather_code == 61
    evening = forecast.window(EVENING)
    assert evening is not None
    assert evening.temperature == pytest.approx(3.5)
    assert evening.wind_speed == pytest.approx(9.7)
    assert evening.weather_code == 0
    # whole-day numbers feed the rules table (zero crossing, umbrella, wind)
    assert forecast.temperature_min == -5.0
    assert forecast.temperature_max == 9.0
    assert forecast.precipitation_total == pytest.approx(2.5)
    assert forecast.wind_speed_max == pytest.approx(9.7)


async def test_null_hours_are_skipped_in_aggregation(ometeo_server) -> None:
    server, base_url = ometeo_server
    temps = [5.0] * 24
    temps[6] = -1.0
    temps[7] = None  # hour dropped from the mean
    temps[8] = 1.0
    temps[9] = 2.0
    temps[10] = 3.0
    temps[11] = 4.0  # morning mean over 5 valid hours = 1.8
    apparent = [2.0] * 24
    apparent[6] = None
    apparent[7] = -4.0
    apparent[8] = -2.0
    apparent[9] = -1.0
    apparent[10] = 0.0
    apparent[11] = 1.0  # mean over 5 valid hours = -1.2
    server.payload = make_entry(temps=temps, apparent=apparent)

    snapshot = await make_provider(base_url, points=(_POINTS[0],)).fetch()

    morning = snapshot.primary.window(MORNING)
    assert morning is not None
    assert morning.temperature == pytest.approx(1.8)
    assert morning.apparent_temperature == pytest.approx(-1.2)
    assert snapshot.primary.temperature_max == 5.0


async def test_window_without_any_data_is_absent(ometeo_server) -> None:
    server, base_url = ometeo_server
    temps: list[float | None] = [5.0] * 24
    temps[6:12] = [None] * 6  # the whole morning is null
    server.payload = make_entry(temps=temps)

    snapshot = await make_provider(base_url, points=(_POINTS[0],)).fetch()

    assert snapshot.primary.window(MORNING) is None
    assert snapshot.primary.window(DAY) is not None
    assert snapshot.primary.window(EVENING) is not None


async def test_failed_regional_point_is_dropped(ometeo_server) -> None:
    server, base_url = ometeo_server
    points = _POINTS[:3]
    server.payload = [
        make_entry(),
        make_entry(),
        make_entry(temps=[None] * 24),  # Тихвин is "down"
    ]

    snapshot = await make_provider(base_url, points=points).fetch()

    # the post goes out without the failed region city (plan section 4)
    assert [f.point.name for f in snapshot.regional] == ["Гатчина"]


async def test_failed_primary_point_raises_data_error(ometeo_server) -> None:
    server, base_url = ometeo_server
    server.payload = [make_entry(temps=[None] * 24)] + [
        make_entry() for _ in _POINTS[1:]
    ]

    with pytest.raises(WeatherDataError, match="Санкт-Петербург"):
        await make_provider(base_url).fetch()


async def test_http_error_becomes_infra_error(ometeo_server) -> None:
    server, base_url = ometeo_server
    server.status = 500
    server.body = "gateway exploded"

    with pytest.raises(WeatherInfraError, match="500"):
        await make_provider(base_url).fetch()


async def test_http_error_includes_api_reason(ometeo_server) -> None:
    server, base_url = ometeo_server
    server.status = 400
    server.body = '{"error": true, "reason": "Cannot initialize. Invalid timezone"}'

    with pytest.raises(WeatherInfraError, match="Invalid timezone"):
        await make_provider(base_url).fetch()


async def test_non_json_success_body_becomes_infra_error(ometeo_server) -> None:
    server, base_url = ometeo_server
    server.body = "<html>not json</html>"

    with pytest.raises(WeatherInfraError, match="not JSON"):
        await make_provider(base_url).fetch()


async def test_location_count_mismatch_raises_data_error(ometeo_server) -> None:
    server, base_url = ometeo_server
    server.payload = [make_entry() for _ in _POINTS[:-1]]  # 6 of 7

    with pytest.raises(WeatherDataError, match="6 locations"):
        await make_provider(base_url).fetch()


async def test_object_reply_for_many_points_raises_data_error(ometeo_server) -> None:
    server, base_url = ometeo_server
    server.payload = make_entry()  # bare object while 7 points requested

    with pytest.raises(WeatherDataError, match="single location object"):
        await make_provider(base_url).fetch()


async def test_missing_hourly_section_raises_data_error(ometeo_server) -> None:
    server, base_url = ometeo_server
    entry = make_entry()
    del entry["hourly"]
    server.payload = [entry] + [make_entry() for _ in _POINTS[1:]]

    with pytest.raises(WeatherDataError, match="hourly"):
        await make_provider(base_url).fetch()


async def test_series_length_mismatch_raises_data_error(ometeo_server) -> None:
    server, base_url = ometeo_server
    entry = make_entry()
    entry["hourly"]["temperature_2m"] = entry["hourly"]["temperature_2m"][:10]
    server.payload = [entry] + [make_entry() for _ in _POINTS[1:]]

    with pytest.raises(WeatherDataError, match="temperature_2m"):
        await make_provider(base_url).fetch()


async def test_bad_time_stamp_raises_data_error(ometeo_server) -> None:
    server, base_url = ometeo_server
    entry = make_entry()
    entry["hourly"]["time"][6] = "yesterday"
    server.payload = [entry] + [make_entry() for _ in _POINTS[1:]]

    with pytest.raises(WeatherDataError, match="time stamp"):
        await make_provider(base_url).fetch()


class _FakeResponse:
    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self._body = body

    async def text(self) -> str:
        return self._body

    async def __aenter__(self) -> _FakeResponse:
        return self

    async def __aexit__(self, *args: Any) -> bool:
        return False


class _RecordingSession:
    """Duck-typed session capturing get() kwargs."""

    def __init__(self, status: int = 200, body: str = "{}") -> None:
        self._status = status
        self._body = body
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, **kwargs: Any) -> _FakeResponse:
        self.calls.append({"url": url, **kwargs})
        return _FakeResponse(self._status, self._body)


class _RaisingSession:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def get(self, url: str, **kwargs: Any) -> _FakeResponse:
        raise self._exc


async def test_constructor_timeout_applies_to_request() -> None:
    session = _RecordingSession(body=json.dumps([make_entry()]))
    provider = OpenMeteoProvider(
        (_POINTS[0],), session=session, timeout=11, base_url="https://example.test"
    )

    await provider.fetch()

    assert len(session.calls) == 1
    assert session.calls[0]["timeout"].total == 11
    assert session.calls[0]["params"]["forecast_days"] == "1"
    assert session.calls[0]["params"]["latitude"] == str(_POINTS[0].latitude)


async def test_shared_session_is_used_not_created() -> None:
    session = _RecordingSession(body=json.dumps([make_entry()]))
    provider = OpenMeteoProvider(
        (_POINTS[0],), session=session, base_url="https://example.test"
    )

    with patch("max_news_bot.weather.open_meteo.aiohttp.ClientSession") as factory:
        snapshot = await provider.fetch()

    factory.assert_not_called()
    assert len(session.calls) == 1
    assert snapshot.primary.point.name == _POINTS[0].name


async def test_injected_session_is_not_closed(ometeo_server) -> None:
    server, base_url = ometeo_server
    server.payload = [make_entry()]
    async with aiohttp.ClientSession() as session:
        provider = make_provider(base_url, points=(_POINTS[0],), session=session)

        await provider.fetch()

        assert not session.closed
    assert len(server.requests) == 1


async def test_network_failure_wrapped_into_infra_error() -> None:
    session = _RaisingSession(aiohttp.ClientConnectionError("connection reset"))
    provider = OpenMeteoProvider(
        (_POINTS[0],), session=session, base_url="https://example.test"
    )

    with pytest.raises(WeatherInfraError) as exc_info:
        await provider.fetch()

    assert isinstance(exc_info.value.__cause__, aiohttp.ClientConnectionError)


async def test_timeout_wrapped_into_infra_error() -> None:
    session = _RaisingSession(asyncio.TimeoutError())
    provider = OpenMeteoProvider(
        (_POINTS[0],), session=session, base_url="https://example.test"
    )

    with pytest.raises(WeatherInfraError) as exc_info:
        await provider.fetch()

    assert isinstance(exc_info.value.__cause__, asyncio.TimeoutError)


def test_empty_points_rejected() -> None:
    with pytest.raises(ValueError, match="at least one point"):
        OpenMeteoProvider(())


def test_provider_satisfies_the_protocol() -> None:
    provider = OpenMeteoProvider((_POINTS[0],), base_url="https://example.test")

    assert isinstance(provider, WeatherProvider)


def test_error_taxonomy() -> None:
    assert issubclass(WeatherInfraError, WeatherProviderError)
    assert issubclass(WeatherDataError, WeatherProviderError)
