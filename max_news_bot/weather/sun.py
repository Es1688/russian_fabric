"""Sunrise and sunset for the weather post (plan section 4).

Pure NOAA sunrise equation — no API call, no external dependency. Plan
section 4 demands exactly that: the sun line must not depend on any
provider, not even on Open-Meteo extras.

Accuracy of the formula is about 1-2 minutes for the region's latitudes,
which is well below what a reader can notice in a "HH:MM" line.
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone as dt_timezone
from math import acos, asin, cos, degrees, floor, radians, sin
from zoneinfo import ZoneInfo

# Julian epoch J2000.0 (2000-01-01 12:00 TT) and the Unix-epoch offset.
_J2000 = 2451545.0
_JD_TO_UNIX = 2440587.5
# Earth's axial tilt and the sun's upper-limb/refraction zenith (-0.833°).
_OBLIQUITY = radians(23.4397)
_ZENITH = radians(-0.833)


def sun_times(
    day: date,
    latitude: float,
    longitude: float,
    tz: ZoneInfo | None = None,
) -> tuple[time | None, time | None]:
    """Return (sunrise, sunset) local times for the given date and point.

    Both values are None when the sun never crosses the horizon that day
    (polar day or polar night) — irrelevant for the channel's points
    (all below the polar circle) but the formula stays honest.
    """
    # Julian day of the requested date at 12:00 UTC.
    julian_day = day.toordinal() + 1721425.0
    days_since_epoch = julian_day - _J2000

    mean_solar_time = days_since_epoch - longitude / 360.0
    mean_anomaly = (357.5291 + 0.98560028 * mean_solar_time) % 360.0
    equation_of_center = (
        1.9148 * sin(radians(mean_anomaly))
        + 0.02 * sin(radians(2 * mean_anomaly))
        + 0.0003 * sin(radians(3 * mean_anomaly))
    )
    ecliptic_longitude = (
        mean_anomaly + equation_of_center + 180.0 + 102.9372
    ) % 360.0

    declination = asin(sin(radians(ecliptic_longitude)) * sin(_OBLIQUITY))
    hour_angle_cos = (
        sin(_ZENITH) - sin(radians(latitude)) * sin(declination)
    ) / (cos(radians(latitude)) * cos(declination))
    if hour_angle_cos > 1.0 or hour_angle_cos < -1.0:
        # Sun always above or always below the horizon.
        return None, None
    hour_angle = degrees(acos(hour_angle_cos))

    # Solar noon in Julian days, then sunrise/sunset around it.
    transit_jd = (
        _J2000
        + mean_solar_time
        + 0.0053 * sin(radians(mean_anomaly))
        - 0.0069 * sin(radians(2 * ecliptic_longitude))
    )
    rise_unix = (transit_jd - hour_angle / 360.0 - _JD_TO_UNIX) * 86400.0
    set_unix = (transit_jd + hour_angle / 360.0 - _JD_TO_UNIX) * 86400.0

    zone = tz if tz is not None else dt_timezone.utc
    rise = datetime.fromtimestamp(rise_unix, tz=zone).time()
    setting = datetime.fromtimestamp(set_unix, tz=zone).time()
    return rise, setting


def day_length_seconds(
    day: date, latitude: float, longitude: float
) -> float | None:
    """Length of the daylight period in seconds; None on polar days."""
    rise, setting = sun_times(day, latitude, longitude)
    if rise is None or setting is None:
        return None
    rise_dt = datetime.combine(day, rise.replace(second=0, microsecond=0))
    set_dt = datetime.combine(day, setting.replace(second=0, microsecond=0))
    return float(floor((set_dt - rise_dt).total_seconds()))
