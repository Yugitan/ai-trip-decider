"""天气 Provider 实现。"""

from __future__ import annotations

from app.providers.weather.base import (
    WeatherProvider,
    WeatherSnapshot,
    WeatherThresholds,
    classify_weather,
    weather_from_daily,
)
from app.providers.weather.null import NullWeatherProvider
from app.providers.weather.open_meteo import OpenMeteoWeatherProvider

__all__ = [
    "NullWeatherProvider",
    "OpenMeteoWeatherProvider",
    "WeatherProvider",
    "WeatherSnapshot",
    "WeatherThresholds",
    "classify_weather",
    "weather_from_daily",
]
