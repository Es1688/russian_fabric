"""Weather tip rules table (plan section 4).

Tips come from a table of rules — not from the LLM. The plan names the
families: umbrella and wind, ice (a freeze-thaw crossing with
precipitation), heat, temperature swings, seasonal. Every rule fires on
provider data only; each rendered post carries at most two tips.

Rule conditions are evaluated over the whole snapshot (all points), not
just St. Petersburg: a tip triggered by Tikhvin weather is still useful
to the channel's region-wide audience. Temperature swings use the
primary point, where the detailed forecast sets reader expectations.

Precipitation is detected from the WMO weather codes (the provider's
dominant code per daypart) backed by measured mm: the Open-Meteo free
tier carries no precipitation probability, so codes + sums are the
shared truth across providers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from max_news_bot.weather.base import PointForecast, WeatherSnapshot
from max_news_bot.weather.format import format_temp, snapshot_date

# The post carries 1-2 tips (plan section 4).
MAX_TIPS = 2

# Rule thresholds. North-western scale: "heat" starts far below Sochi
# numbers, a 12 m/s wind is already a warning for older readers.
WIND_TIP_MS = 12.0  # the day's max wind that earns a tip
HEAT_TIP_C = 27.0  # the day's max temperature that counts as heat here
FROST_TIP_C = -18.0  # the day's min temperature that counts as severe frost
SWING_TIP_C = 8.0  # same-day min->max spread that earns a tip
UMBRELLA_MM = 0.5  # day's measured precipitation that earns a tip
ICE_MM = 0.2  # freeze-thaw needs actual wet ground

# WMO weather interpretation codes with precipitation (drizzle, rain,
# freezing rain, snow, showers, thunderstorms). No probability is
# available, so the code is the "expect rain" signal.
PRECIPITATION_CODES = frozenset(
    {
        51, 53, 55,  # drizzle
        56, 57,  # freezing drizzle
        61, 63, 65,  # rain
        66, 67,  # freezing rain
        71, 73, 75,  # snowfall
        77,  # snow grains
        80, 81, 82,  # rain showers
        85, 86,  # snow showers
        95, 96, 99,  # thunderstorms
    }
)
SNOW_CODES = frozenset({71, 73, 75, 77, 85, 86})


def is_precipitation_code(code: int) -> bool:
    """True when the WMO code promises measurable precipitation."""
    return code in PRECIPITATION_CODES


def is_snow_code(code: int) -> bool:
    """True when the WMO code promises snow (not rain)."""
    return code in SNOW_CODES


@dataclass(frozen=True)
class WeatherTipRule:
    """One row of the tips table: priority order decides which tips win."""

    name: str
    priority: int
    condition: Callable[[WeatherSnapshot], bool]
    text: Callable[[WeatherSnapshot], str]


def _has_wet_window(point: PointForecast) -> bool:
    for window in point.windows:
        if window.precipitation >= ICE_MM or is_precipitation_code(window.weather_code):
            return True
    return point.precipitation_total >= ICE_MM


def _is_ice(snapshot: WeatherSnapshot) -> bool:
    # Freeze-thaw: the day crosses zero with wet precipitation — the
    # classic north-western black-ice morning (plan section 4).
    for point in snapshot.points:
        if point.temperature_min < 0 < point.temperature_max:
            if _has_wet_window(point):
                return True
    return False


def _is_wind(snapshot: WeatherSnapshot) -> bool:
    return any(point.wind_speed_max >= WIND_TIP_MS for point in snapshot.points)


def _is_heat(snapshot: WeatherSnapshot) -> bool:
    return any(point.temperature_max >= HEAT_TIP_C for point in snapshot.points)


def _is_frost(snapshot: WeatherSnapshot) -> bool:
    return any(point.temperature_min <= FROST_TIP_C for point in snapshot.points)


def _is_swing(snapshot: WeatherSnapshot) -> bool:
    primary = snapshot.primary
    return primary.temperature_max - primary.temperature_min >= SWING_TIP_C


def _is_umbrella(snapshot: WeatherSnapshot) -> bool:
    for point in snapshot.points:
        if point.precipitation_total >= UMBRELLA_MM:
            return True
        if any(
            is_precipitation_code(window.weather_code) for window in point.windows
        ):
            return True
    return False


def _swing_text(snapshot: WeatherSnapshot) -> str:
    primary = snapshot.primary
    return (
        f"За день температура заметно меняется — от "
        f"{format_temp(primary.temperature_min)}° до "
        f"{format_temp(primary.temperature_max)}°. Одевайтесь слоями, "
        "чтобы легко снять лишнее."
    )


# Seasonal tips by month (1-12): the north-west calendar for older
# readers. The seasonal rule is the fallback — a calm, unremarkable day
# still gets one useful line, so the post always carries 1-2 tips.
SEASONAL_TIPS: dict[int, str] = {
    1: "Морозные дни: одевайтесь теплее и не выходите на лёд водоёмов без нужды.",
    2: "Дни уже заметно длиннее, но утром ещё скользко — обувь с нескользящей подошвой.",
    3: "Днём пригревает солнце, а утром лужи подмерзают — ступайте осторожнее.",
    4: "Снег сходит, на улицах сыро — пригодится сухая сменная обувь.",
    5: "Дачный сезон начинается: не спешите с посадками, ночные заморозки на северо-западе обычны.",
    6: "Начался сезон клещей и комаров: на прогулке в лесу прикрывайте руки и голову.",
    7: "Тёплые дни: гуляйте в тени в самые жаркие часы и носите с собой воду.",
    8: "Ночи становятся прохладными — вечером на даче пригодится тёплая кофта.",
    9: "Дачный сезон на северо-западе короткий: успейте собрать урожай до дождей.",
    10: "Дожди и листопад: выбирайте непромокаемую обувь, а ступени у дома держите сухими.",
    11: "Дни короткие: выходите на прогулку пораньше, пока светло.",
    12: "Стемнеет рано: возвращайтесь с прогулки засветло и одевайтесь теплее к вечеру.",
}


def _seasonal_text(snapshot: WeatherSnapshot) -> str:
    return SEASONAL_TIPS[snapshot_date(snapshot).month]


def _always(_snapshot: WeatherSnapshot) -> bool:
    return True


# The table itself. Priority (lower = stronger): safety of older readers
# first (ice, wind, heat, frost), then comfort (swing, umbrella), the
# seasonal line last as the guaranteed fallback.
TIP_RULES: tuple[WeatherTipRule, ...] = (
    WeatherTipRule(
        name="ice",
        priority=10,
        condition=_is_ice,
        text=lambda _s: (
            "Ночью подморозит, а днём мокро — возможен гололёд. "
            "Надевайте обувь с нескользящей подошвой."
        ),
    ),
    WeatherTipRule(
        name="wind",
        priority=20,
        condition=_is_wind,
        text=lambda _s: (
            "Ожидается сильный ветер: держите зонт сложенным, "
            "окна дома закрывайте плотнее."
        ),
    ),
    WeatherTipRule(
        name="heat",
        priority=30,
        condition=_is_heat,
        text=lambda _s: (
            "Днём жарко: гуляйте в тени, пейте больше воды "
            "и не задерживайтесь на солнце."
        ),
    ),
    WeatherTipRule(
        name="frost",
        priority=40,
        condition=_is_frost,
        text=lambda _s: (
            "Ожидаются сильные морозы: одевайтесь в несколько слоёв "
            "и не задерживайтесь на улице надолго."
        ),
    ),
    WeatherTipRule(
        name="temp_swing",
        priority=50,
        condition=_is_swing,
        text=_swing_text,
    ),
    WeatherTipRule(
        name="umbrella",
        priority=60,
        condition=_is_umbrella,
        text=lambda _s: "Возможен дождь — возьмите с собой зонт или дождевик.",
    ),
    WeatherTipRule(
        name="seasonal",
        priority=90,
        condition=_always,
        text=_seasonal_text,
    ),
)


def pick_tips(snapshot: WeatherSnapshot) -> tuple[str, ...]:
    """Return 1-2 tip texts: rules table order, strongest first."""
    triggered = [rule for rule in TIP_RULES if rule.condition(snapshot)]
    triggered.sort(key=lambda rule: rule.priority)
    return tuple(rule.text(snapshot) for rule in triggered[:MAX_TIPS])


def triggered_rule_names(snapshot: WeatherSnapshot) -> tuple[str, ...]:
    """Names of all triggered rules (before the two-tip cut) — for tests
    and for the filter log."""
    triggered = [rule for rule in TIP_RULES if rule.condition(snapshot)]
    triggered.sort(key=lambda rule: rule.priority)
    return tuple(rule.name for rule in triggered)
