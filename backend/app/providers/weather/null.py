"""天气降级实现：返回空预报。

天气只影响偏好权重，不影响可行性 —— 所以"没有天气"是**可以直接跳过**的降级，
而不是错误（PRD §15.5：天气 API 失败 → 跳过天气适配，不显示天气建议）。
"""

from __future__ import annotations

from app.providers.base import LatLng, ProviderHealth
from app.providers.weather.base import WeatherSnapshot

__all__ = ["NullWeatherProvider"]


class NullWeatherProvider:
    name = "disabled"

    def health(self) -> ProviderHealth:
        return ProviderHealth(
            name=self.name, available=False, detail="未启用天气（跳过天气适配）", degraded=True
        )

    async def forecast(self, location: LatLng, *, days: int = 7) -> list[WeatherSnapshot]:
        return []
