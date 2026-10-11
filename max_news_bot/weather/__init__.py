"""Weather rubric domain (plan section 4): provider contract, Open-Meteo
batch provider, met.no fallback with the two-tier chain, tips rules table,
post template, sun formula, validator, slot pipeline adapter."""

from max_news_bot.weather.base import (
    DAY,
    EVENING,
    MORNING,
    WINDOW_LABELS,
    PointForecast,
    WeatherDataError,
    WeatherInfraError,
    WeatherProvider,
    WeatherProviderError,
    WeatherSnapshot,
    WeatherWindow,
)
from max_news_bot.weather.fallback import FallbackWeatherProvider
from max_news_bot.weather.met_no import MetNoProvider
from max_news_bot.weather.open_meteo import OpenMeteoProvider
from max_news_bot.weather.pipeline import WeatherPipeline
from max_news_bot.weather.rules import pick_tips
from max_news_bot.weather.sun import sun_times
from max_news_bot.weather.template import render_weather_post
from max_news_bot.weather.validator import validate_weather_post

__all__ = [
    "DAY",
    "EVENING",
    "MORNING",
    "WINDOW_LABELS",
    "FallbackWeatherProvider",
    "MetNoProvider",
    "OpenMeteoProvider",
    "PointForecast",
    "WeatherDataError",
    "WeatherInfraError",
    "WeatherProvider",
    "WeatherProviderError",
    "WeatherSnapshot",
    "WeatherWindow",
    "WeatherPipeline",
    "pick_tips",
    "render_weather_post",
    "sun_times",
    "validate_weather_post",
]
