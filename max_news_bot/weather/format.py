"""Shared number formatting for the weather post (plan section 4).

The template renders provider values into digits and the validator
re-reads those digits back — both must agree on rounding and signs, so
the helpers live in one module instead of being copied.
"""

from __future__ import annotations

import math
from datetime import date

from max_news_bot.weather.base import WeatherSnapshot


def round_half_up(value: float) -> int:
    """Round to the nearest integer, halves toward +infinity.

    Python's ``round`` is banker's rounding (+2.5 -> 2, -2.5 -> -2);
    a single shared helper keeps the rendered digits stable on both the
    writing and the checking side.
    """
    return math.floor(value + 0.5)


def format_temp(value: float) -> str:
    """Render a temperature as an integer with an explicit sign: +7, 0, -3."""
    rounded = round_half_up(value)
    return f"+{rounded}" if rounded > 0 else str(rounded)


def snapshot_date(snapshot: WeatherSnapshot) -> date:
    """The forecast day of the snapshot (its points share one local day)."""
    return date.fromisoformat(snapshot.primary.date)
