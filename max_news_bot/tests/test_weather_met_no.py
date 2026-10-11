from __future__ import annotations

import asyncio
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
    WeatherSnapshot,
)
from max_news_bot.weather.met_no import MetNoProvider


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

_DATE = "2026-10-10"  # the local (Europe/Moscow = UTC+3) day of the payload


def make_metno_payload(
    *,
    hours_utc: list[int] | None = None,
    temps: list[float | None] | None = None,
    symbols: list[str | None] | None = None,
    precips: list[float | None] | None = None,
    winds: list[float | None] | None = None,
    humidities: list[float | None] | None = None,
    date: str = _DATE,
) -> dict[str, Any]:
    """A locationforecast/2.0 reply; unset series get flat defaults.

    ``symbols[i] is None`` omits ``next_1_hours`` for that step entirely —
    the API trims it at the range edges."""
    if hours_utc is None:
        hours_utc = list(range(24))
    count = len(hours_utc)
    series_temps: list[float | None] = temps if temps is not None else [5.0] * count
    series_symbols: list[str | None] = symbols if symbols is not None else ["cloudy"] * count
    series_precip: list[float | None] = precips if precips is not None else [0.0] * count
    series_wind: list[float | None] = winds if winds is not None else [2.0] * count
    series_rh: list[float | None] = humidities if humidities is not None else [60.0] * count
    timeseries: list[dict[str, Any]] = []
    for index, hour in enumerate(hours_utc):
        data: dict[str, Any] = {
            "instant": {
                "details": {
                    "air_temperature": series_temps[index],
                    "wind_speed": series_wind[index],
                    "relative_humidity": series_rh[index],
                }
            }
        }
        if series_symbols[index] is not None:
            data["next_1_hours"] = {
                "summary": {"symbol_code": series_symbols[index]},
                "details": {"precipitation_amount": series_precip[index]},
            }
        timeseries.append({"time": f"{date}T{hour:02d}:00:00Z", "data": data})
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [30.3, 59.9]},
        "properties": {
            "meta": {"updated_at": f"{date}T02:00:00Z", "units": {}},
            "timeseries": timeseries,
        },
    }


class FakeMetNoServer:
    """Local aiohttp server impersonating api.met.no.

    Replies are switched per point latitude (rounded to the provider's
    four decimals); points without an override get ``default_payload``."""

    def __init__(self, default_payload: Any) -> None:
        self.requests: list[dict[str, Any]] = []
        self.default_payload = default_payload
        self.default_status = 200
        self.default_body: str | None = None
        self.default_delay = 0.0
        # round(lat, 4) -> {"status", "body", "payload", "delay"}
        self.by_lat: dict[float, dict[str, Any]] = {}
        self._runner: web.AppRunner | None = None

    async def start(self) -> str:
        async def forecast(request: web.Request) -> web.Response:
            self.requests.append(
                {
                    "query": dict(request.query),
                    "user_agent": request.headers.get("User-Agent"),
                }
            )
            lat = round(float(request.query["lat"]), 4)
            route = self.by_lat.get(
                lat,
                {
                    "status": self.default_status,
                    "body": self.default_body,
                    "payload": self.default_payload,
                    "delay": self.default_delay,
                },
            )
            if route.get("delay"):
                await asyncio.sleep(route["delay"])
            if route.get("body") is not None:
                return web.Response(
                    status=route["status"], text=route["body"],
                    content_type="application/json",
                )
            return web.json_response(route["payload"], status=route["status"])

        app = web.Application()
        app.router.add_get("/weatherapi/locationforecast/2.0/compact", forecast)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        assert runner.addresses, "test server did not bind"
        port = runner.addresses[0][1]
        self._runner = runner
        return f"http://127.0.0.1:{port}/weatherapi/locationforecast/2.0/compact"

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()


@pytest.fixture
async def metno_server():
    server = FakeMetNoServer(make_metno_payload())
    base_url = await server.start()
    yield server, base_url
    await server.stop()


def make_provider(base_url: str, points=..., **overrides: Any) -> MetNoProvider:
    params: dict[str, Any] = {
        "points": _POINTS if points is ... else points,
        "base_url": base_url,
        "session": None,
        "timeout": 5,
    }
    params.update(overrides)
    return MetNoProvider(**params)


async def test_one_request_per_point_with_own_coordinates(metno_server) -> None:
    server, base_url = metno_server

    snapshot = await make_provider(base_url).fetch()

    # no batch form: seven separate HTTP transactions (plan section 4)
    assert len(server.requests) == 7
    requested_lats = {call["query"]["lat"] for call in server.requests}
    requested_lons = {call["query"]["lon"] for call in server.requests}
    assert requested_lats == {f"{point.latitude:.4f}" for point in _POINTS}
    assert requested_lons == {f"{point.longitude:.4f}" for point in _POINTS}
    assert isinstance(snapshot, WeatherSnapshot)
    assert snapshot.primary.point.name == "Санкт-Петербург"
    assert [f.point.name for f in snapshot.regional] == [p.name for p in _POINTS[1:]]


async def test_user_agent_sent_on_every_request(metno_server) -> None:
    server, base_url = metno_server

    await make_provider(base_url).fetch()

    # the Yr terms of service require an identifying User-Agent
    assert len(server.requests) == 7
    assert all(
        call["user_agent"] and "content-fabric-max-bot" in call["user_agent"]
        for call in server.requests
    )


async def test_custom_user_agent_is_used(metno_server) -> None:
    server, base_url = metno_server

    await make_provider(base_url, user_agent="unit-test-agent/1.0").fetch()

    assert {call["user_agent"] for call in server.requests} == {"unit-test-agent/1.0"}


async def test_windows_and_day_aggregates_parsed(metno_server) -> None:
    server, base_url = metno_server
    # Stamps are UTC; Europe/Moscow is UTC+3, so local morning 06-11 is
    # UTC 3-8, day 12-16 is UTC 9-13, evening 17-22 is UTC 14-19. UTC
    # 21-23 is already the next local day and must stay out of the post.
    temps = (
        [-5.0, -4.0, -3.0]  # UTC 0-2 -> local night 03-05
        + [-2.0, -1.0, 0.0, 1.0, 2.0, 3.0]  # UTC 3-8 -> morning, mean 0.5
        + [4.0, 5.0, 6.0, 7.0, 8.0]  # UTC 9-13 -> day, mean 6.0
        + [7.0, 6.0, 5.0, 4.0, 3.0, 2.0]  # UTC 14-19 -> evening, mean 4.5
        + [1.0, 0.0, -30.0, -40.0]  # UTC 20 -> local 23; UTC 21-23 -> next day
    )
    precip = [0.0] * 24
    precip[10] = 0.6  # local 13:00
    precip[11] = 0.8  # local 14:00 -> day window sums 1.4
    precip[20] = 0.5  # local 23:00: outside every window, but in day totals
    wind = [2.0] * 24
    wind[15] = 9.7  # local 18:00 -> evening max
    symbols = ["cloudy"] * 24
    symbols[4] = "lightrain"  # one rainy morning hour, cloudy still dominates
    symbols[10] = "rain"
    symbols[11] = "rain"
    symbols[12] = "rain"  # day: rain dominates (3 of 5)
    symbols[14:20] = ["clearsky_day"] * 6  # suffix stripped -> WMO 0
    server.default_payload = make_metno_payload(
        temps=temps, precips=precip, winds=wind, symbols=symbols
    )

    snapshot = await make_provider(base_url, points=(_POINTS[0],)).fetch()
    forecast = snapshot.primary

    assert forecast.date == _DATE
    morning = forecast.window(MORNING)
    assert morning is not None
    assert morning.temperature == pytest.approx(0.5)
    assert morning.precipitation == 0.0
    assert morning.weather_code == 3  # cloudy dominates the morning
    day = forecast.window(DAY)
    assert day is not None
    assert day.temperature == pytest.approx(6.0)
    assert day.precipitation == pytest.approx(1.4)
    assert day.weather_code == 63  # "rain"
    evening = forecast.window(EVENING)
    assert evening is not None
    assert evening.temperature == pytest.approx(4.5)
    assert evening.wind_speed == pytest.approx(9.7)
    assert evening.weather_code == 0  # "clearsky_day" -> 0
    # whole-day numbers feed the rules table; the next-day tail (-30/-40)
    # never leaks into them
    assert forecast.temperature_min == -5.0
    assert forecast.temperature_max == 8.0
    assert forecast.precipitation_total == pytest.approx(1.9)
    assert forecast.wind_speed_max == pytest.approx(9.7)


async def test_apparent_temperature_formula(metno_server) -> None:
    server, base_url = metno_server
    # met.no has no "feels like" field: the provider computes the BOM
    # apparent temperature locally (same no-API approach as the sun formula).
    temps = [10.0] * 24
    winds = [3.0] * 24
    humidities = [70.0] * 24
    server.default_payload = make_metno_payload(
        temps=temps, winds=winds, humidities=humidities
    )
    vapour = 0.70 * 6.105 * (2.718281828459045 ** (17.27 * 10.0 / (237.7 + 10.0)))
    expected = 10.0 + 0.33 * vapour - 0.70 * 3.0 - 4.00

    snapshot = await make_provider(base_url, points=(_POINTS[0],)).fetch()

    morning = snapshot.primary.window(MORNING)
    assert morning is not None
    assert morning.apparent_temperature == pytest.approx(expected, abs=0.06)


async def test_missing_humidity_falls_back_to_plain_temperature(
    metno_server,
) -> None:
    server, base_url = metno_server
    server.default_payload = make_metno_payload(
        temps=[10.0] * 24, humidities=[None] * 24
    )

    snapshot = await make_provider(base_url, points=(_POINTS[0],)).fetch()

    morning = snapshot.primary.window(MORNING)
    assert morning is not None
    assert morning.apparent_temperature == pytest.approx(10.0)


async def test_steps_without_next_1_hours_contribute_nothing(metno_server) -> None:
    server, base_url = metno_server
    symbols = ["cloudy"] * 24
    precips = [0.0] * 24
    for hour in range(3, 9):  # the whole morning has no next_1_hours block
        symbols[hour] = None
        precips[hour] = 9.9  # would poison the window if misattributed

    server.default_payload = make_metno_payload(symbols=symbols, precips=precips)

    snapshot = await make_provider(base_url, points=(_POINTS[0],)).fetch()

    morning = snapshot.primary.window(MORNING)
    assert morning is not None
    assert morning.precipitation == 0.0
    assert morning.weather_code == 0  # no codes in the window at all


async def test_unknown_symbol_is_skipped_not_fatal(metno_server) -> None:
    server, base_url = metno_server
    symbols = ["rain"] * 24
    symbols[5] = "brand_new_symbol_the_map_does_not_know"

    server.default_payload = make_metno_payload(symbols=symbols)

    snapshot = await make_provider(base_url, points=(_POINTS[0],)).fetch()

    morning = snapshot.primary.window(MORNING)
    assert morning is not None
    assert morning.weather_code == 63  # the known hours still decide


async def test_regional_point_http_error_is_dropped(metno_server) -> None:
    server, base_url = metno_server
    server.by_lat[round(_POINTS[2].latitude, 4)] = {
        "status": 500,
        "body": "upstream boom",
        "payload": None,
        "delay": 0,
    }

    snapshot = await make_provider(base_url).fetch()

    # the post goes out without the failed region city (plan section 4)
    assert [f.point.name for f in snapshot.regional] == [
        p.name for p in _POINTS[1:] if p.name != "Выборг"
    ]
    assert len(server.requests) == 7  # every point was still attempted


async def test_primary_point_http_error_raises_infra(metno_server) -> None:
    server, base_url = metno_server
    server.by_lat[round(_POINTS[0].latitude, 4)] = {
        "status": 503,
        "body": "maintenance",
        "payload": None,
        "delay": 0,
    }

    with pytest.raises(WeatherInfraError, match="503"):
        await make_provider(base_url).fetch()


async def test_primary_empty_timeseries_raises_data_error(metno_server) -> None:
    server, base_url = metno_server
    server.default_payload = make_metno_payload(hours_utc=[])

    with pytest.raises(WeatherDataError, match="Санкт-Петербург"):
        await make_provider(base_url, points=(_POINTS[0],)).fetch()


async def test_primary_null_temperatures_raise_data_error(metno_server) -> None:
    server, base_url = metno_server
    server.default_payload = make_metno_payload(temps=[None] * 24)

    with pytest.raises(WeatherDataError, match="no usable met.no data"):
        await make_provider(base_url, points=(_POINTS[0],)).fetch()


async def test_hanging_regional_point_burns_only_its_own_timeout(
    metno_server,
) -> None:
    server, base_url = metno_server
    # Луга hangs for far longer than the per-point budget: its timeout must
    # neither stretch the whole fetch nor cancel the other six points.
    server.by_lat[round(_POINTS[4].latitude, 4)] = {
        "status": 200,
        "body": None,
        "payload": make_metno_payload(),
        "delay": 1.0,
    }

    snapshot = await asyncio.wait_for(
        make_provider(base_url, timeout=1).fetch(), timeout=5
    )

    assert snapshot.primary.point.name == "Санкт-Петербург"
    assert [f.point.name for f in snapshot.regional] == [
        p.name for p in _POINTS[1:] if p.name != "Луга"
    ]


async def test_non_json_success_body_becomes_infra_error(metno_server) -> None:
    server, base_url = metno_server
    server.default_body = "<html>not json</html>"

    with pytest.raises(WeatherInfraError, match="not JSON"):
        await make_provider(base_url, points=(_POINTS[0],)).fetch()


async def test_bad_time_stamp_raises_data_error(metno_server) -> None:
    server, base_url = metno_server
    payload = make_metno_payload()
    payload["properties"]["timeseries"][4]["time"] = "not-a-date"
    server.default_payload = payload

    with pytest.raises(WeatherDataError, match="time stamp"):
        await make_provider(base_url, points=(_POINTS[0],)).fetch()


async def test_time_stamp_without_timezone_raises_data_error(metno_server) -> None:
    server, base_url = metno_server
    payload = make_metno_payload()
    payload["properties"]["timeseries"][4]["time"] = "2026-10-10T06:00:00"
    server.default_payload = payload

    with pytest.raises(WeatherDataError, match="no timezone"):
        await make_provider(base_url, points=(_POINTS[0],)).fetch()


async def test_missing_properties_raises_data_error(metno_server) -> None:
    server, base_url = metno_server
    server.default_payload = {"type": "Feature"}

    with pytest.raises(WeatherDataError, match="properties"):
        await make_provider(base_url, points=(_POINTS[0],)).fetch()


async def test_missing_instant_details_raise_data_error(metno_server) -> None:
    server, base_url = metno_server
    payload = make_metno_payload()
    del payload["properties"]["timeseries"][4]["data"]["instant"]
    server.default_payload = payload

    with pytest.raises(WeatherDataError, match="instant details"):
        await make_provider(base_url, points=(_POINTS[0],)).fetch()


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


async def test_per_point_timeout_and_headers_reach_every_request() -> None:
    session = _RecordingSession(body=json.dumps(make_metno_payload()))

    provider = MetNoProvider(
        _POINTS, session=session, timeout=13, base_url="https://example.test"
    )
    await provider.fetch()

    # one call per point, each with its own timeout budget and the UA
    assert len(session.calls) == 7
    assert {call["timeout"].total for call in session.calls} == {13}
    assert {call["headers"]["User-Agent"] for call in session.calls} == {
        "content-fabric-max-bot/0.1 (https://github.com/Es1688/content_fabric_max)"
    }
    requested = {(call["params"]["lat"], call["params"]["lon"]) for call in session.calls}
    assert requested == {
        (f"{point.latitude:.4f}", f"{point.longitude:.4f}") for point in _POINTS
    }


async def test_shared_session_is_used_not_created() -> None:
    session = _RecordingSession(body=json.dumps(make_metno_payload()))
    provider = MetNoProvider(
        (_POINTS[0],), session=session, base_url="https://example.test"
    )

    with patch("max_news_bot.weather.met_no.aiohttp.ClientSession") as factory:
        snapshot = await provider.fetch()

    factory.assert_not_called()
    assert len(session.calls) == 1
    assert snapshot.primary.point.name == _POINTS[0].name


async def test_injected_session_is_not_closed(metno_server) -> None:
    server, base_url = metno_server
    async with aiohttp.ClientSession() as session:
        provider = make_provider(base_url, points=(_POINTS[0],), session=session)

        await provider.fetch()

        assert not session.closed
    assert len(server.requests) == 1


async def test_network_failure_wrapped_into_infra_error() -> None:
    session = _RaisingSession(aiohttp.ClientConnectionError("connection reset"))
    provider = MetNoProvider(
        (_POINTS[0],), session=session, base_url="https://example.test"
    )

    with pytest.raises(WeatherInfraError) as exc_info:
        await provider.fetch()

    assert isinstance(exc_info.value.__cause__, aiohttp.ClientConnectionError)


async def test_timeout_wrapped_into_infra_error() -> None:
    session = _RaisingSession(asyncio.TimeoutError())
    provider = MetNoProvider(
        (_POINTS[0],), session=session, base_url="https://example.test"
    )

    with pytest.raises(WeatherInfraError) as exc_info:
        await provider.fetch()

    assert isinstance(exc_info.value.__cause__, asyncio.TimeoutError)


def test_empty_points_rejected() -> None:
    with pytest.raises(ValueError, match="at least one point"):
        MetNoProvider(())


def test_unknown_timezone_rejected() -> None:
    with pytest.raises(ValueError, match="unknown timezone"):
        MetNoProvider((_POINTS[0],), timezone_name="Mars/Olympus")


def test_provider_satisfies_the_protocol() -> None:
    provider = MetNoProvider((_POINTS[0],), base_url="https://example.test")

    assert isinstance(provider, WeatherProvider)
