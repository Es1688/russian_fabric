"""Weather post validator (plan section 4).

Three checks beyond the generic length validator:

- plausibility for every point: temperatures, wind and weather codes
  inside realistic ranges, feels-like near the actual temperature, daily
  min/max consistent with the daypart windows;
- numbers match: every signed number in the text (°, m/s, %) must be a
  rounded value from the snapshot, and the sun times must equal the pure
  formula output for the primary point;
- forbidden topics: magnetic storms and pressure are never part of the
  channel's weather post (plan section 4) — mentioning them is a reject.

Follows the writers/validator.py contract: ``(ok, reason)`` with a
readable cause, so the slot pipeline logs it and skips the slot (weather
has no reserve post).
"""

from __future__ import annotations

import re
from zoneinfo import ZoneInfo

from max_news_bot.weather.base import WeatherSnapshot
from max_news_bot.weather.format import round_half_up, snapshot_date
from max_news_bot.weather.sun import sun_times
from max_news_bot.writers.validator import visible_text

MAX_POST_CHARS = 4000  # MAX hard limit, same value as config/rubrics.py

# Realistic bounds for the region's provider data; anything outside is a
# broken payload (unit mix-ups, Fahrenheit leaks), not record weather.
TEMP_MIN_PLAUSIBLE_C = -60.0
TEMP_MAX_PLAUSIBLE_C = 50.0
APPARENT_MAX_DROP_C = 25.0  # wind chill can cut deep, but not deeper
APPARENT_MAX_RISE_C = 15.0  # humid heat raises feels-like, somewhat
WIND_MAX_PLAUSIBLE_MS = 60.0
# WMO weather interpretation codes live in 0..99.
_CODE_MAX = 99
# Day parts may sit slightly outside [temp_min, temp_max] because the
# provider aggregates them from different hourly slices (means vs min/max).
PART_VS_DAILY_SLACK_C = 3.0

# Numbers are only trusted when tied to a unit: "+7°", "5 м/с", "(60%)".
# Bare digits (day of month in the header, tip texts) are prose.
_MEASURED_NUMBER_RE = re.compile(r"([+-]?\d+)\s*(?:°|м/с|%)")
_TIME_RE = re.compile(r"(\d{1,2}:\d{2})")
# Unicode minus/dash normalize to ASCII so a pasted "−2°" is checked too.
_MINUS_TRANSLATION = str.maketrans("\u2212\u2013", "--")

# Plan section 4: magnetic storms and pressure are out of scope. Match
# word stems, not whole words, to cover all declensions.
_FORBIDDEN_STEMS = ("магнитн", "давлени", "рт. ст", "гпа")

_WINDOW_LABELS_RU = {"morning": "утро", "day": "день", "evening": "вечер"}


def check_plausibility(snapshot: WeatherSnapshot) -> tuple[str, ...]:
    """Return a problem description per implausible point (empty = fine)."""
    problems: list[str] = []
    for point in snapshot.points:
        where = point.point.name
        for window in point.windows:
            label = _WINDOW_LABELS_RU.get(window.label, window.label)
            if not TEMP_MIN_PLAUSIBLE_C <= window.temperature <= TEMP_MAX_PLAUSIBLE_C:
                problems.append(
                    f"{where}, {label}: температура {window.temperature}° "
                    "вне правдоподобного диапазона"
                )
            drop = window.temperature - window.apparent_temperature
            if drop > APPARENT_MAX_DROP_C or -drop > APPARENT_MAX_RISE_C:
                problems.append(
                    f"{where}, {label}: «ощущается как» "
                    f"{window.apparent_temperature}° слишком далеко от "
                    f"{window.temperature}°"
                )
            if not 0.0 <= window.wind_speed <= WIND_MAX_PLAUSIBLE_MS:
                problems.append(
                    f"{where}, {label}: ветер {window.wind_speed} м/с "
                    "вне правдоподобного диапазона"
                )
            if not 0 <= window.weather_code <= _CODE_MAX:
                problems.append(
                    f"{where}, {label}: код погоды {window.weather_code} "
                    "вне диапазона WMO 0–99"
                )
        for name, value in (
            ("минимум дня", point.temperature_min),
            ("максимум дня", point.temperature_max),
        ):
            if not TEMP_MIN_PLAUSIBLE_C <= value <= TEMP_MAX_PLAUSIBLE_C:
                problems.append(
                    f"{where}: {name} {value}° вне правдоподобного диапазона"
                )
        if point.temperature_min > point.temperature_max:
            problems.append(
                f"{where}: минимум дня {point.temperature_min}° выше "
                f"максимума {point.temperature_max}°"
            )
        for window in point.windows:
            label = _WINDOW_LABELS_RU.get(window.label, window.label)
            if (
                window.temperature
                < point.temperature_min - PART_VS_DAILY_SLACK_C
                or window.temperature
                > point.temperature_max + PART_VS_DAILY_SLACK_C
            ):
                problems.append(
                    f"{where}, {label}: температура {window.temperature}° "
                    "не согласуется с дневными "
                    f"{point.temperature_min}°…{point.temperature_max}°"
                )
    return tuple(problems)


def _allowed_numbers(snapshot: WeatherSnapshot) -> set[int]:
    """Every signed integer the template may legitimately print."""
    allowed: set[int] = set()
    for point in snapshot.points:
        for window in point.windows:
            allowed.add(round_half_up(window.temperature))
            allowed.add(round_half_up(window.apparent_temperature))
            allowed.add(round_half_up(window.wind_speed))
        allowed.add(round_half_up(point.temperature_min))
        allowed.add(round_half_up(point.temperature_max))
        allowed.add(round_half_up(point.wind_speed_max))
        allowed.add(round_half_up(point.precipitation_total))
    return allowed


def check_numbers_match(
    text: str, snapshot: WeatherSnapshot, *, tz_name: str = "Europe/Moscow"
) -> tuple[str, ...]:
    """Every measured number in the text must come from the snapshot."""
    problems: list[str] = []
    normalized = text.translate(_MINUS_TRANSLATION)

    allowed = _allowed_numbers(snapshot)
    for match in _MEASURED_NUMBER_RE.finditer(normalized):
        number = int(match.group(1))
        if number not in allowed:
            problems.append(f"число {match.group(0)!r} отсутствует в данных погоды")

    # Sun times must equal the pure formula for the primary point.
    primary = snapshot.primary
    expected_rise, expected_set = sun_times(
        snapshot_date(snapshot),
        primary.point.latitude,
        primary.point.longitude,
        ZoneInfo(tz_name),
    )
    times = {match.group(1) for match in _TIME_RE.finditer(normalized)}
    for label, expected in (("восход", expected_rise), ("закат", expected_set)):
        if expected is not None and f"{expected:%H:%M}" not in times:
            problems.append(f"время {label}а не совпадает с расчётом")

    # Every point of the snapshot must be present: a silently dropped
    # town would pass the number check while the post loses a line.
    for point in snapshot.points:
        if point.point.name not in text:
            problems.append(f"точка {point.point.name!r} пропущена в тексте")

    return tuple(problems)


def validate_weather_post(
    text: str,
    snapshot: WeatherSnapshot,
    *,
    max_chars: int = MAX_POST_CHARS,
    tz_name: str = "Europe/Moscow",
) -> tuple[bool, str | None]:
    """Full weather post check: length, plausibility, numbers, topics."""
    visible_len = len(visible_text(text))
    if visible_len > MAX_POST_CHARS:
        return False, f"too long: {visible_len} > {MAX_POST_CHARS} (hard limit)"
    if visible_len > max_chars:
        return False, f"too long: {visible_len} > {max_chars}"

    problems = list(check_plausibility(snapshot))
    problems.extend(check_numbers_match(text, snapshot, tz_name=tz_name))

    lowered = text.lower()
    for stem in _FORBIDDEN_STEMS:
        if stem in lowered:
            problems.append(f"запрещённая тема: {stem!r}")

    if problems:
        return False, "; ".join(problems)
    return True, None
