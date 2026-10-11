from __future__ import annotations

from max_news_bot.tests.weather_builders import (
    GATCHINA,
    NEUTRAL_DAY,
    SPB,
    point,
    snapshot,
    window,
)
from max_news_bot.weather.base import DAY, MORNING
from max_news_bot.weather.rules import (
    MAX_TIPS,
    SEASONAL_TIPS,
    TIP_RULES,
    is_precipitation_code,
    is_snow_code,
    pick_tips,
    triggered_rule_names,
)

# WMO codes used across the rule tests.
CLEAR = 1
RAIN = 61
DRIZZLE = 51
SNOW = 71
THUNDERSTORM = 95


def test_rule_names_unique_and_prioritized() -> None:
    names = [rule.name for rule in TIP_RULES]
    assert len(names) == len(set(names))
    priorities = [rule.priority for rule in TIP_RULES]
    assert priorities == sorted(priorities)


# --- umbrella --------------------------------------------------------------


def test_umbrella_on_rain_code() -> None:
    snap = snapshot(point(windows=(window(weather_code=RAIN),)))
    assert "umbrella" in triggered_rule_names(snap)


def test_umbrella_on_measured_precipitation_without_code() -> None:
    # Hourly drizzle can round to a sum under a dry dominant code.
    snap = snapshot(
        point(
            windows=(window(weather_code=CLEAR),),
            precipitation_total=0.6,
        )
    )
    assert "umbrella" in triggered_rule_names(snap)


def test_no_umbrella_on_dry_day() -> None:
    snap = snapshot()  # neutral builder: dry, calm
    assert "umbrella" not in triggered_rule_names(snap)


def test_region_point_triggers_umbrella() -> None:
    # Tips serve the whole audience: rain in Gatchina warns SPb readers
    # travelling across the oblast too.
    snap = snapshot(
        point(),
        (point(GATCHINA, windows=(window(weather_code=RAIN),)),),
    )
    assert "umbrella" in triggered_rule_names(snap)


# --- ice (freeze-thaw with precipitation) ----------------------------------


def test_ice_on_freeze_thaw_with_precipitation() -> None:
    snap = snapshot(
        point(
            windows=(window(MORNING, temperature=-2.0, weather_code=RAIN),),
            temperature_min=-2.5,
            temperature_max=3.0,
        )
    )
    assert "ice" in triggered_rule_names(snap)


def test_ice_on_freeze_thaw_with_snow_code() -> None:
    snap = snapshot(
        point(
            windows=(window(temperature=-1.0, weather_code=SNOW),),
            temperature_min=-2.0,
            temperature_max=1.0,
        )
    )
    assert "ice" in triggered_rule_names(snap)


def test_no_ice_without_precipitation() -> None:
    snap = snapshot(
        point(
            windows=(window(weather_code=CLEAR),),
            temperature_min=-2.5,
            temperature_max=3.0,
        )
    )
    assert "ice" not in triggered_rule_names(snap)


def test_no_ice_without_zero_crossing() -> None:
    # All-day frost with snow is winter weather, not freeze-thaw ice.
    snap = snapshot(
        point(
            windows=(window(temperature=-5.0, weather_code=SNOW),),
            temperature_min=-6.0,
            temperature_max=-2.0,
        )
    )
    assert "ice" not in triggered_rule_names(snap)


# --- wind, heat, frost, swings ---------------------------------------------


def test_wind_on_strong_wind() -> None:
    snap = snapshot(point(wind_speed_max=13.0))
    assert "wind" in triggered_rule_names(snap)


def test_no_wind_on_moderate_breeze() -> None:
    snap = snapshot(point(wind_speed_max=8.0))
    assert "wind" not in triggered_rule_names(snap)


def test_heat_on_hot_day() -> None:
    snap = snapshot(
        point(
            windows=(window(temperature=28.0, apparent_temperature=30.0),),
            temperature_max=28.0,
        )
    )
    assert "heat" in triggered_rule_names(snap)


def test_no_heat_below_threshold() -> None:
    snap = snapshot(
        point(
            windows=(window(temperature=26.0, apparent_temperature=27.0),),
            temperature_max=26.0,
        )
    )
    assert "heat" not in triggered_rule_names(snap)


def test_frost_on_severe_cold() -> None:
    snap = snapshot(
        point(
            windows=(window(temperature=-19.0, weather_code=SNOW),),
            temperature_min=-19.5,
            temperature_max=-14.0,
        )
    )
    assert "frost" in triggered_rule_names(snap)


def test_temp_swing_on_large_daily_spread() -> None:
    snap = snapshot(
        point(
            windows=(
                window(MORNING, temperature=-1.0),
                window(DAY, temperature=8.0),
            ),
            temperature_min=-1.5,
            temperature_max=8.0,
        )
    )
    assert "temp_swing" in triggered_rule_names(snap)


def test_no_temp_swing_on_steady_day() -> None:
    snap = snapshot()  # 3..6 degrees across the day
    assert "temp_swing" not in triggered_rule_names(snap)


# --- seasonal fallback and the two-tip cut ---------------------------------


def test_neutral_day_gets_only_seasonal_tip() -> None:
    snap = snapshot()
    assert triggered_rule_names(snap) == ("seasonal",)
    tips = pick_tips(snap)
    assert tips == (SEASONAL_TIPS[10],)


def test_seasonal_tip_follows_month() -> None:
    snap = snapshot(point(date="2026-12-15"))
    assert pick_tips(snap) == (SEASONAL_TIPS[12],)


def test_seasonal_tips_cover_all_twelve_months() -> None:
    assert sorted(SEASONAL_TIPS) == list(range(1, 13))


def test_at_most_two_tips() -> None:
    # Everything fires at once: ice, wind, swing, umbrella, seasonal.
    snap = snapshot(
        point(
            windows=(
                window(MORNING, temperature=-2.0, weather_code=RAIN, wind_speed=13.0),
                window(DAY, temperature=8.0, weather_code=RAIN),
            ),
            temperature_min=-2.5,
            temperature_max=8.0,
            wind_speed_max=13.0,
        )
    )
    assert len(triggered_rule_names(snap)) >= MAX_TIPS + 1
    tips = pick_tips(snap)
    assert len(tips) == MAX_TIPS


def test_safety_rules_outrank_comfort() -> None:
    # Ice beats umbrella and the seasonal line even when all trigger.
    snap = snapshot(
        point(
            windows=(
                window(MORNING, temperature=-2.0, weather_code=RAIN),
                window(DAY, temperature=3.0, weather_code=RAIN),
            ),
            temperature_min=-2.5,
            temperature_max=3.0,
        )
    )
    names = triggered_rule_names(snap)
    assert names.index("ice") < names.index("umbrella") < names.index("seasonal")
    tips = pick_tips(snap)
    assert tips[0].startswith("Ночью подморозит")  # the ice tip


def test_swing_tip_carries_the_day_numbers() -> None:
    snap = snapshot(
        point(
            windows=(window(MORNING, temperature=-2.0), window(DAY, temperature=6.5)),
            temperature_min=-2.0,
            temperature_max=6.5,
        )
    )
    tips = pick_tips(snap)
    assert any("от -2° до +7°" in tip for tip in tips)


# --- WMO code classification ------------------------------------------------


def test_precipitation_codes() -> None:
    for code in (51, 61, 66, 71, 80, 85, 95):
        assert is_precipitation_code(code), code
    for code in (0, 1, 2, 3, 45, 48):
        assert not is_precipitation_code(code), code


def test_snow_codes() -> None:
    for code in (71, 73, 75, 77, 85, 86):
        assert is_snow_code(code), code
    for code in (51, 61, 80, 95):
        assert not is_snow_code(code), code


def test_neutral_day_uses_channel_snapshot_defaults() -> None:
    # Guard the builder itself: the neutral day must stay neutral, other
    # tests depend on it.
    snap = snapshot()
    assert snap.primary.point == SPB
    assert snap.primary.date == NEUTRAL_DAY
