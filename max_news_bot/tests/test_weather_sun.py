from __future__ import annotations

from datetime import date, time
from zoneinfo import ZoneInfo

from max_news_bot.weather.sun import day_length_seconds, sun_times

MSK = ZoneInfo("Europe/Moscow")
# Channel points: SPb and the northernmost region town (Vyborg).
SPB = (59.9386, 30.3141)
VYBORG = (60.7104, 28.7519)
MURMANSK = (68.97, 33.08)  # above the polar circle: polar day/night checks


def _minutes(value: time) -> int:
    return value.hour * 60 + value.minute


def test_spb_reference_days() -> None:
    # Reference values cross-checked with the independent ``ephem``
    # astronomy library (dev machine, not part of the test run): the
    # formula stays within ~2 minutes of it on these dates.
    expected = {
        date(2026, 1, 1): (time(10, 0), time(16, 3)),
        date(2026, 6, 21): (time(3, 35), time(22, 25)),
        date(2026, 10, 10): (time(7, 24), time(18, 6)),
        date(2026, 12, 21): (time(10, 0), time(15, 53)),
    }
    for day, (exp_rise, exp_set) in expected.items():
        rise, setting = sun_times(day, *SPB, tz=MSK)
        assert rise is not None and setting is not None, day
        assert abs(_minutes(rise) - _minutes(exp_rise)) <= 3, (day, rise)
        assert abs(_minutes(setting) - _minutes(exp_set)) <= 3, (day, setting)


def test_sunrise_before_sunset() -> None:
    rise, setting = sun_times(date(2026, 10, 10), *SPB, tz=MSK)
    assert rise is not None and setting is not None
    assert _minutes(rise) < _minutes(setting)


def test_solar_noon_near_one_pm_msk() -> None:
    # SPb longitude 30.31°E with MSK = UTC+3 puts the true solar noon
    # around 12:45-13:00 local; a wrong timezone sign would move it by
    # two hours.
    rise, setting = sun_times(date(2026, 10, 10), *SPB, tz=MSK)
    assert rise is not None and setting is not None
    noon = (_minutes(rise) + _minutes(setting)) / 2
    assert 12 * 60 + 30 <= noon <= 13 * 60 + 15, noon


def test_day_length_seasonal_order() -> None:
    june = day_length_seconds(date(2026, 6, 21), *SPB)
    october = day_length_seconds(date(2026, 10, 10), *SPB)
    december = day_length_seconds(date(2026, 12, 21), *SPB)
    assert june is not None and october is not None and december is not None
    assert june > october > december
    # ~18.8 h in June vs ~5.9 h in December at 59.94°N.
    assert june > 18 * 3600
    assert december < 6.5 * 3600


def test_polar_day_and_night_return_none() -> None:
    # Murmansk in June: the sun never sets; in December it never rises.
    assert sun_times(date(2026, 6, 21), *MURMANSK, tz=MSK) == (None, None)
    assert sun_times(date(2026, 12, 21), *MURMANSK, tz=MSK) == (None, None)


def test_channel_northernmost_point_never_polar() -> None:
    # Vyborg (60.71°N) is the northernmost channel point and stays well
    # below the polar circle: the post always has its sun line.
    rise, setting = sun_times(date(2026, 6, 21), *VYBORG, tz=MSK)
    assert rise is not None
    assert setting is not None


def test_utc_when_no_timezone_given() -> None:
    rise_utc, _ = sun_times(date(2026, 10, 10), *SPB)
    rise_msk, _ = sun_times(date(2026, 10, 10), *SPB, tz=MSK)
    assert rise_utc is not None and rise_msk is not None
    # MSK is UTC+3: the same event is 3 hours earlier in UTC terms.
    assert abs(_minutes(rise_msk) - _minutes(rise_utc) - 180) <= 1
