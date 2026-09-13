"""Provider 装配工厂。

装配规则（与 ``Settings`` 的 ``*_effective`` 属性一一对应）：
- 有 Key 且显式指定 → 真实 Provider；
- 无 Key → 对应的降级实现（null / seed_only / haversine），**绝不报错**；
- 显式指定但缺 Key → 自动回退到"次优但可用"的实现（如 amap → osrm）。

业务层只依赖 :class:`Providers` 这一个值对象；换厂商 = 改这个文件 + 加一个 Provider。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.config import LimitsConfig, Settings, get_limits_config, get_settings, get_ttl_config
from app.providers.base import ProviderHealth
from app.providers.llm.base import LlmProvider
from app.providers.llm.deepseek import DeepSeekProvider
from app.providers.llm.null import NullLlmProvider
from app.providers.map.amap import AmapMapProvider
from app.providers.map.base import MapProvider
from app.providers.map.haversine import HaversineMapProvider
from app.providers.map.osrm import OsrmMapProvider
from app.providers.search.base import SearchProvider
from app.providers.search.seed_only import SeedOnlyProvider
from app.providers.search.tavily import TavilyProvider
from app.providers.weather.base import WeatherProvider, WeatherThresholds
from app.providers.weather.null import NullWeatherProvider
from app.providers.weather.open_meteo import OpenMeteoWeatherProvider

__all__ = ["Providers", "build_providers"]


@dataclass(frozen=True, slots=True)
class Providers:
    """一次装配出来的四类能力。``close()`` 由应用关闭时调用（当前实现按需建连）。"""

    llm: LlmProvider
    search: SearchProvider
    map: MapProvider
    weather: WeatherProvider

    def health(self) -> dict[str, ProviderHealth]:
        return {
            "llm": self.llm.health(),
            "search": self.search.health(),
            "map": self.map.health(),
            "weather": self.weather.health(),
        }

    def degraded_modes(self) -> list[str]:
        """只返回**处于降级**的能力，格式与 ``Settings.degraded_modes()`` 一致。"""
        return [f"{key}:{health.detail or health.name}" for key, health in self.health().items() if health.degraded]


def build_providers(
    settings: Settings | None = None, *, limits: LimitsConfig | None = None
) -> Providers:
    cfg = settings or get_settings()
    lim = limits or get_limits_config()
    return Providers(
        llm=_build_llm(cfg),
        search=_build_search(cfg),
        map=_build_map(cfg, lim),
        weather=_build_weather(cfg),
    )


def _build_llm(settings: Settings) -> LlmProvider:
    if settings.llm_provider_effective == "deepseek" and settings.deepseek_api_key:
        return DeepSeekProvider(
            settings.deepseek_api_key,
            base_url=settings.llm_base_url,
            models={
                "fast": settings.llm_tier_fast_model,
                "strong": settings.llm_tier_strong_model,
            },
            timeout_s=settings.llm_timeout_s,
            max_retries=settings.llm_max_retries,
        )
    return NullLlmProvider()


def _build_search(settings: Settings) -> SearchProvider:
    if settings.search_provider_effective == "tavily" and settings.tavily_api_key:
        return TavilyProvider(settings.tavily_api_key)
    return SeedOnlyProvider()


def _build_map(settings: Settings, limits: LimitsConfig) -> MapProvider:
    effective = settings.map_provider_effective
    if effective == "amap" and settings.amap_web_key:
        return AmapMapProvider(settings.amap_web_key)
    if effective == "haversine":
        return HaversineMapProvider(limits.travel_modes)
    return OsrmMapProvider(settings.osrm_base_url)


def _build_weather(settings: Settings) -> WeatherProvider:
    if settings.weather_provider != "open_meteo":
        return NullWeatherProvider()
    thresholds_raw = get_ttl_config().refresh_policy.get("weather_thresholds") or {}
    thresholds = WeatherThresholds(
        rain_probability=float(thresholds_raw.get("rain_probability", 0.60)),
        high_temp_c=float(thresholds_raw.get("high_temp_c", 33)),
        low_temp_c=float(thresholds_raw.get("low_temp_c", 8)),
    )
    return OpenMeteoWeatherProvider(thresholds=thresholds)
