"""open-meteo 天气 Provider（免费、无需 Key）。

选它是因为无 Key 即可用 —— 符合"缺 Key 也能端到端运行"的架构铁律。
只取三个 daily 字段（降雨概率、最高/最低温），够做偏好调整，
不取逐小时数据（没有决策价值，徒增体积与延迟）。
"""

from __future__ import annotations

import httpx

from app.core.errors import ErrorCode, ProviderError
from app.providers.base import LatLng, ProviderHealth
from app.providers.weather.base import WeatherSnapshot, WeatherThresholds, weather_from_daily

__all__ = ["OpenMeteoWeatherProvider"]

_ENDPOINT = "https://api.open-meteo.com/v1/forecast"
_DAILY_FIELDS = "precipitation_probability_max,temperature_2m_max,temperature_2m_min"


class OpenMeteoWeatherProvider:
    name = "open_meteo"

    def __init__(
        self,
        *,
        timezone: str = "Asia/Shanghai",
        timeout_s: float = 8.0,
        thresholds: WeatherThresholds | None = None,
    ) -> None:
        self._timezone = timezone
        self._timeout_s = timeout_s
        self._thresholds = thresholds or WeatherThresholds()

    def health(self) -> ProviderHealth:
        return ProviderHealth(name=self.name, available=True, detail="免费天气服务（无需 Key）")

    async def forecast(self, location: LatLng, *, days: int = 7) -> list[WeatherSnapshot]:
        params: dict[str, str | int] = {
            "latitude": f"{location.lat:.6f}",
            "longitude": f"{location.lng:.6f}",
            "daily": _DAILY_FIELDS,
            "timezone": self._timezone,
            "forecast_days": max(1, min(16, days)),
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout_s) as client:
                response = await client.get(_ENDPOINT, params=params)
        except httpx.TimeoutException as exc:
            raise ProviderError(
                self.name, "forecast", kind=ErrorCode.PROVIDER_TIMEOUT, message="天气请求超时"
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(
                self.name,
                "forecast",
                kind=ErrorCode.PROVIDER_UNAVAILABLE,
                message=f"天气请求失败：{type(exc).__name__}",
            ) from exc

        if response.status_code >= 400:
            raise ProviderError(
                self.name,
                "forecast",
                kind=ErrorCode.PROVIDER_UNAVAILABLE,
                message=f"天气服务返回 HTTP {response.status_code}",
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise ProviderError(
                self.name,
                "forecast",
                kind=ErrorCode.PROVIDER_INVALID_RESPONSE,
                message="天气服务返回了非 JSON 响应",
            ) from exc
        daily = body.get("daily") if isinstance(body, dict) else None
        if not isinstance(daily, dict):
            raise ProviderError(
                self.name,
                "forecast",
                kind=ErrorCode.PROVIDER_INVALID_RESPONSE,
                message="天气响应缺少 daily",
            )
        return weather_from_daily(daily, source=self.name, thresholds=self._thresholds)
