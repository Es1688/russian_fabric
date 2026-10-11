from __future__ import annotations

from max_news_bot.tests.weather_builders import (
    GATCHINA,
    VYBORG,
    full_snapshot,
    point,
    snapshot,
    window,
)
from max_news_bot.weather.base import DAY, EVENING, MORNING
from max_news_bot.weather.format import format_temp
from max_news_bot.weather.template import (
    describe_precipitation,
    render_weather_post,
)

CLEAR = 1
RAIN = 61  # slight rain
MODERATE_RAIN = 63
HEAVY_RAIN = 65
SNOW = 71  # slight snowfall
MODERATE_SNOW = 73
THUNDERSTORM = 95


# --- post structure (plan section 4) ----------------------------------------


def test_post_structure_spb_in_detail_plus_region_lines() -> None:
    post = render_weather_post(full_snapshot())

    assert post.startswith("🌤 Погода — Санкт-Петербург и область")
    assert "суббота, 10 октября" in post  # header date line
    # SPb block: morning / afternoon / evening with feels-like, precip, wind
    assert "Санкт-Петербург\n" in post
    assert "Утро: +3°, ощущается как" in post
    assert "День: +6°, ощущается как" in post
    assert "Вечер: +4°, ощущается как" in post
    # One line per region town
    assert "По области:" in post
    for name in ("Гатчина", "Выборг", "Тихвин"):
        assert f"{name}: днём" in post
    # Tips and the sun line
    assert "Советы:" in post
    assert "Восход 07:24, закат 18:06" in post


def test_post_numbers_come_from_the_snapshot() -> None:
    primary = point(
        windows=(
            window(MORNING, temperature=2.4, apparent_temperature=0.1, wind_speed=3.4),
            window(DAY, temperature=6.8, apparent_temperature=4.9, wind_speed=5.4),
            window(EVENING, temperature=4.0, apparent_temperature=2.2, wind_speed=4.1),
        )
    )
    post = render_weather_post(snapshot(primary))
    assert f"Утро: {format_temp(2.4)}°, ощущается как {format_temp(0.1)}°" in post
    assert "ветер 3 м/с" in post


def test_injected_tips_render_verbatim() -> None:
    post = render_weather_post(
        full_snapshot(), tips=("Первый совет.", "Второй совет.")
    )
    assert "— Первый совет." in post
    assert "— Второй совет." in post


def test_post_fits_max_limit() -> None:
    post = render_weather_post(full_snapshot())
    assert len(post) <= 4000


# --- degradation (plan section 4) -------------------------------------------


def test_missing_morning_window_drops_only_that_line() -> None:
    primary = point(
        windows=(
            window(DAY, temperature=6.0),
            window(EVENING, temperature=4.0),
        )
    )
    post = render_weather_post(snapshot(primary))
    assert "Утро:" not in post
    assert "День:" in post
    assert "Вечер:" in post


def test_region_line_without_afternoon_falls_back_to_any_window() -> None:
    gatchina = point(GATCHINA, windows=(window(MORNING, temperature=2.0),))
    post = render_weather_post(full_snapshot(regional=(gatchina,)))
    assert "Гатчина: днём +2°" in post


def test_region_point_set_to_empty() -> None:
    # Only SPb survived: no "По области:" section header.
    post = render_weather_post(snapshot(point()))
    assert "По области:" not in post


# --- precipitation wording ----------------------------------------------------


def test_precipitation_wording_matrix() -> None:
    cases = (
        (window(weather_code=CLEAR, precipitation=0.0), "без осадков"),
        # dry dominant code but measurable mm -> still rain, mm intensity
        (window(weather_code=CLEAR, precipitation=0.3), "небольшой дождь"),
        (window(weather_code=CLEAR, precipitation=3.0), "дождь"),
        (window(weather_code=CLEAR, precipitation=6.0), "сильный дождь"),
        # the WMO code carries the intensity class
        (window(weather_code=RAIN, precipitation=0.0), "небольшой дождь"),
        (window(weather_code=MODERATE_RAIN, precipitation=2.5), "дождь"),
        (window(weather_code=HEAVY_RAIN, precipitation=6.0), "сильный дождь"),
        (window(weather_code=SNOW, precipitation=1.0, temperature=-3.0), "небольшой снег"),
        (window(weather_code=MODERATE_SNOW, precipitation=3.0, temperature=-3.0), "снег"),
        (window(weather_code=THUNDERSTORM, precipitation=4.0), "сильный дождь"),
    )
    for part, expected in cases:
        assert describe_precipitation(part) == expected, part


def test_snow_wording_appears_in_post_on_winter_day() -> None:
    primary = point(
        date="2026-01-15",
        windows=(
            window(MORNING, temperature=-6.0, weather_code=SNOW, precipitation=1.5),
            window(DAY, temperature=-4.0, weather_code=MODERATE_SNOW, precipitation=2.5),
            window(EVENING, temperature=-7.0, weather_code=SNOW, precipitation=1.0),
        ),
        temperature_min=-7.5,
        temperature_max=-3.5,
        precipitation_total=5.0,
    )
    post = render_weather_post(snapshot(primary))
    assert "Утро: -6°" in post
    assert "небольшой снег" in post
    # January 15, 2026 is a Thursday.
    assert "четверг, 15 января" in post


def test_sun_line_present_for_every_season() -> None:
    # The sun formula must never give up on the channel's points.
    primary = point(date="2026-06-21")
    post = render_weather_post(snapshot(primary))
    assert "Восход" in post and "закат" in post


def test_sun_times_follow_primary_point_coordinates() -> None:
    # Same day, different longitude (Vyborg vs SPb) -> different sun line.
    as_primary = point(VYBORG)
    post = render_weather_post(snapshot(as_primary))
    # Vyborg rises earlier and sets later than SPb in October (further
    # west and slightly north); assert it is not the SPb line.
    assert "Восход 07:24, закат 18:06" not in post
