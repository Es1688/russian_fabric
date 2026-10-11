"""Weather post template (plan section 4).

Structure: St. Petersburg in detail (morning / afternoon / evening,
feels-like, precipitation, wind), one line per region town, 1-2 tips
from the rules table, sunrise and sunset from the pure formula.

Every number in the output comes from the provider data — the LLM is not
involved, and the validator re-checks each rendered digit against the
snapshot. Russian weekday/month names are inlined dictionaries: the
runtime locale is not guaranteed on the VPS container.

A missing daypart window is degraded gracefully: the line simply drops
out, the rest of the post stays (mirrors the provider's own degradation
for failed points, plan section 4).
"""

from __future__ import annotations

from zoneinfo import ZoneInfo

from max_news_bot.weather.base import (
    DAY,
    EVENING,
    MORNING,
    PointForecast,
    WeatherSnapshot,
    WeatherWindow,
)
from max_news_bot.weather.format import format_temp, round_half_up, snapshot_date
from max_news_bot.weather.rules import is_precipitation_code, is_snow_code, pick_tips
from max_news_bot.weather.sun import sun_times

_WEEKDAYS_RU = (
    "понедельник",
    "вторник",
    "среда",
    "четверг",
    "пятница",
    "суббота",
    "воскресенье",
)
_MONTHS_RU_GENITIVE = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)

# Part labels in post order; the WMO code carries the intensity class
# (e.g. 61 slight / 63 moderate / 65 heavy rain), mm sums only back it
# up when the dominant code stayed dry.
_PART_LABELS = ((MORNING, "Утро"), (DAY, "День"), (EVENING, "Вечер"))
# Slight-class codes: drizzle, light rain/snow, light showers.
_SLIGHT_CODES = frozenset({51, 56, 61, 66, 71, 77, 80, 85})
# Heavy-class codes: heavy rain/snow/showers and thunderstorms.
_HEAVY_CODES = frozenset({55, 65, 75, 82, 86, 95, 96, 99})
# mm fallback thresholds for a wet sum under a dry dominant code.
_LIGHT_PRECIPITATION_MM = 2.0
_HEAVY_PRECIPITATION_MM = 5.0
# Below this mm sum the part counts as dry unless the code says rain.
_DRY_MM = 0.2


def describe_precipitation(window: WeatherWindow) -> str:
    """Human wording for one daypart's precipitation.

    WMO code and measured mm are reconciled: a dry sum under a rain code
    still says rain (an hour of drizzle rounds to 0.0 mm), a wet sum
    under a dry code still says a light rain.
    """
    mm = window.precipitation
    code = window.weather_code
    wet_code = is_precipitation_code(code)

    if not wet_code and mm < _DRY_MM:
        return "без осадков"

    kind = "снег" if is_snow_code(code) else "дождь"
    if wet_code:
        if code in _HEAVY_CODES:
            return f"сильный {kind}"
        if code in _SLIGHT_CODES:
            return f"небольшой {kind}"
        return kind
    # Dry dominant code but a measurable sum: intensity from mm.
    if mm >= _HEAVY_PRECIPITATION_MM:
        return f"сильный {kind}"
    if mm >= _LIGHT_PRECIPITATION_MM:
        return kind
    return f"небольшой {kind}"


def describe_wind(window: WeatherWindow) -> str:
    """Wind wording: the part's maximum speed in m/s."""
    return f"ветер {round_half_up(window.wind_speed)} м/с"


def _part_line(label: str, window: WeatherWindow) -> str:
    return (
        f"{label}: {format_temp(window.temperature)}°, ощущается как "
        f"{format_temp(window.apparent_temperature)}°, "
        f"{describe_precipitation(window)}, {describe_wind(window)}"
    )


def _region_line(point: PointForecast) -> str:
    # Region towns get one line built on the afternoon window; if the
    # afternoon hours were unusable, fall back to any window the point has.
    window = point.window(DAY) or point.windows[0]
    return (
        f"{point.point.name}: днём {format_temp(window.temperature)}°, "
        f"{describe_precipitation(window)}, {describe_wind(window)}"
    )


def render_weather_post(
    snapshot: WeatherSnapshot,
    *,
    tips: tuple[str, ...] | None = None,
    tz_name: str = "Europe/Moscow",
) -> str:
    """Render the weather post from provider data (no LLM involved).

    ``tips`` is injectable for tests/pipeline; by default the rules table
    picks them.
    """
    primary = snapshot.primary
    if tips is None:
        tips = pick_tips(snapshot)

    day = snapshot_date(snapshot)
    date_line = (
        f"{_WEEKDAYS_RU[day.weekday()]}, {day.day} "
        f"{_MONTHS_RU_GENITIVE[day.month - 1]}"
    )

    lines: list[str] = [
        f"🌤 Погода — {primary.point.name} и область",
        date_line,
        "",
        primary.point.name,
    ]
    for label_key, label_ru in _PART_LABELS:
        window = primary.window(label_key)
        if window is not None:
            lines.append(_part_line(label_ru, window))

    if snapshot.regional:
        lines.append("")
        lines.append("По области:")
        lines.extend(_region_line(point) for point in snapshot.regional)

    if tips:
        lines.append("")
        lines.append("Советы:")
        lines.extend(f"— {tip}" for tip in tips)

    rise, setting = sun_times(
        day,
        primary.point.latitude,
        primary.point.longitude,
        ZoneInfo(tz_name),
    )
    if rise is not None and setting is not None:
        lines.append("")
        lines.append(f"🌅 Восход {rise:%H:%M}, закат {setting:%H:%M}")

    return "\n".join(lines)
