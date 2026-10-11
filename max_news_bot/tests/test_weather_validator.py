from __future__ import annotations

from max_news_bot.tests.weather_builders import (
    GATCHINA,
    VYBORG,
    full_snapshot,
    point,
    snapshot,
    window,
)
from max_news_bot.weather.base import DAY, MORNING
from max_news_bot.weather.template import render_weather_post
from max_news_bot.weather.validator import (
    check_numbers_match,
    check_plausibility,
    validate_weather_post,
)

CLEAR = 1
RAIN = 61


# --- happy path ---------------------------------------------------------------


def test_rendered_post_passes() -> None:
    snap = full_snapshot()
    ok, reason = validate_weather_post(render_weather_post(snap), snap)
    assert ok, reason
    assert reason is None


def test_extreme_but_plausible_winter_day_passes() -> None:
    # -30..-25 with snow is severe but real weather: plausibility must
    # reject broken payloads, not honest frosts.
    snap = snapshot(
        point(
            windows=(
                window(MORNING, temperature=-29.0, weather_code=71, precipitation=1.0),
                window(DAY, temperature=-26.0, weather_code=71, precipitation=2.0),
            ),
            temperature_min=-30.0,
            temperature_max=-25.0,
            precipitation_total=3.0,
            wind_speed_max=9.0,
        )
    )
    assert check_plausibility(snap) == ()
    ok, reason = validate_weather_post(render_weather_post(snap), snap)
    assert ok, reason


# --- plausibility per point (plan section 4) ----------------------------------


def test_fahrenheit_leak_rejected() -> None:
    # 68 "degrees" is a classic unit mix-up: 20°C rendered as Fahrenheit.
    snap = full_snapshot(
        primary=point(windows=(window(temperature=68.0, apparent_temperature=66.0),))
    )
    problems = check_plausibility(snap)
    assert any("температура 68.0" in problem for problem in problems)


def test_absurd_frost_rejected() -> None:
    snap = full_snapshot(
        primary=point(
            windows=(window(temperature=-70.0, apparent_temperature=-75.0),),
            temperature_min=-70.0,
            temperature_max=-65.0,
        )
    )
    assert check_plausibility(snap) != ()


def test_feels_like_too_far_rejected() -> None:
    snap = full_snapshot(
        primary=point(
            windows=(window(temperature=5.0, apparent_temperature=40.0),)
        )
    )
    problems = check_plausibility(snap)
    assert any("ощущается как" in problem for problem in problems)


def test_implausible_wind_rejected() -> None:
    snap = full_snapshot(
        primary=point(windows=(window(wind_speed=75.0),), wind_speed_max=75.0)
    )
    problems = check_plausibility(snap)
    assert any("ветер" in problem for problem in problems)


def test_negative_wind_rejected() -> None:
    snap = full_snapshot(
        primary=point(windows=(window(wind_speed=-5.0),), wind_speed_max=0.0)
    )
    assert check_plausibility(snap) != ()


def test_min_above_max_rejected() -> None:
    snap = full_snapshot(
        primary=point(temperature_min=5.0, temperature_max=2.0)
    )
    problems = check_plausibility(snap)
    assert any("минимум дня" in problem for problem in problems)


def test_window_outside_daily_range_rejected() -> None:
    snap = full_snapshot(
        primary=point(
            windows=(window(temperature=20.0, apparent_temperature=19.0),),
            temperature_min=2.0,
            temperature_max=7.0,
        )
    )
    problems = check_plausibility(snap)
    assert any("не согласуется с дневными" in problem for problem in problems)


def test_bad_weather_code_rejected() -> None:
    snap = full_snapshot(
        primary=point(windows=(window(weather_code=120),))
    )
    problems = check_plausibility(snap)
    assert any("код погоды" in problem for problem in problems)


def test_implausible_region_point_rejected_too() -> None:
    # Plausibility covers every point, not only the primary one.
    snap = full_snapshot(regional=(point(GATCHINA, windows=(window(temperature=55.0),)),))
    problems = check_plausibility(snap)
    assert any(problem.startswith("Гатчина") for problem in problems)


# --- numbers must match the API data (plan section 4) --------------------------


def test_tampered_temperature_rejected() -> None:
    snap = full_snapshot()
    post = render_weather_post(snap).replace("+6°", "+9°", 1)
    problems = check_numbers_match(post, snap)
    assert any("+9°" in problem for problem in problems)
    ok, reason = validate_weather_post(post, snap)
    assert ok is False
    assert "отсутствует в данных погоды" in reason


def test_tampered_wind_rejected() -> None:
    snap = full_snapshot()
    post = render_weather_post(snap).replace("ветер 4 м/с", "ветер 14 м/с", 1)
    ok, reason = validate_weather_post(post, snap)
    assert ok is False
    assert "14 м/с" in reason


def test_tampered_sun_time_rejected() -> None:
    snap = full_snapshot()
    post = render_weather_post(snap).replace("Восход 07:24", "Восход 07:44")
    ok, reason = validate_weather_post(post, snap)
    assert ok is False
    assert "время восхода" in reason


def test_unchanged_post_has_no_number_problems() -> None:
    snap = full_snapshot()
    assert check_numbers_match(render_weather_post(snap), snap) == ()


# --- structure ------------------------------------------------------------------


def test_dropped_town_line_rejected() -> None:
    # A missing town row means the post silently lost a point.
    snap = full_snapshot()
    post = render_weather_post(snap)
    assert "Выборг: днём" in post
    post = post.replace(
        next(
            line
            for line in post.splitlines()
            if line.startswith("Выборг")
        ),
        "Кировск (Лен. обл.): днём +6°, без осадков, ветер 4 м/с",
    )
    ok, reason = validate_weather_post(post, snap)
    assert ok is False
    assert "пропущена в тексте" in reason


def test_unicode_minus_is_checked_like_ascii() -> None:
    snap = full_snapshot()
    post = render_weather_post(snap).replace("+6°", "\u22126°", 1)
    ok, reason = validate_weather_post(post, snap)
    assert ok is False
    assert "отсутствует в данных погоды" in reason


# --- forbidden topics and limits (plan section 4) ---------------------------------


def test_magnetic_storms_topic_rejected() -> None:
    snap = full_snapshot()
    post = render_weather_post(snap) + "\nСегодня возможны магнитные бури."
    ok, reason = validate_weather_post(post, snap)
    assert ok is False
    assert "магнитн" in reason


def test_pressure_topic_rejected() -> None:
    snap = full_snapshot()
    post = render_weather_post(snap) + "\nДавление 750 мм рт. ст."
    ok, reason = validate_weather_post(post, snap)
    assert ok is False
    assert "давлени" in reason


def test_hard_limit_rejected() -> None:
    snap = full_snapshot()
    post = render_weather_post(snap) + "\n" + "п" * 4000
    ok, reason = validate_weather_post(post, snap)
    assert ok is False
    assert "hard limit" in reason


def test_soft_limit_rejected() -> None:
    snap = full_snapshot()
    post = render_weather_post(snap)
    ok, reason = validate_weather_post(post, snap, max_chars=300)
    assert ok is False
    assert "> 300" in reason
