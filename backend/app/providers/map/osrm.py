"""OSRM 地图 Provider。

★ 只信任距离，不信任耗时 ★
公共演示实例（router.project-osrm.org）只部署了 car profile：请求
``/route/v1/foot/...`` 返回的仍是车速（实测 44.6 km/h）。所以：

- 任何模式都返回真实**路网距离**（``distance_source='osrm'``）；
- 只有 ``taxi``（车）才返回 ``duration_min``（``duration_source='osrm'``）；
- 其余模式的 ``duration_min=None``，由调用方按速度假设推导并标记 estimated。

这不是保守，是实测结论 —— 把车速当步行时间会让用户看到"步行 3 分钟"实际走 40 分钟。
"""

from __future__ import annotations

import math
from decimal import Decimal

import httpx

from app.core.errors import ErrorCode, ProviderError
from app.domain.models import TransportMode, TransportSource
from app.providers.base import LatLng, ProviderHealth
from app.providers.map.base import MapLeg

__all__ = ["OsrmMapProvider"]

# OSRM profile 名称（URL 的一部分）。注意：公共实例只认 car profile。
_PROFILE: dict[TransportMode, str] = {
    "taxi": "driving",
    "bike": "driving",
    "walk": "foot",
    "metro": "driving",
    "bus": "driving",
    "ferry": "driving",
}


class OsrmMapProvider:
    name = "osrm"

    def __init__(
        self,
        base_url: str = "https://router.project-osrm.org",
        *,
        timeout_s: float = 8.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s

    def health(self) -> ProviderHealth:
        return ProviderHealth(
            name=self.name,
            available=True,
            detail="真实路网距离（耗时非车行模式为推导值）",
        )

    def estimate_cost(self, op: str, units: int = 1) -> Decimal:
        # 公共 OSRM 实例不计费；真实成本来自自建/商业服务，由成本层另行配置。
        return Decimal("0")

    async def leg(self, origin: LatLng, destination: LatLng, *, mode: TransportMode) -> MapLeg:
        profile = _PROFILE.get(mode, "driving")
        coords = f"{origin.as_lnglat()};{destination.as_lnglat()}"
        url = f"{self._base_url}/route/v1/{profile}/{coords}"
        params = {"overview": "false", "alternatives": "false", "steps": "false"}
        try:
            async with httpx.AsyncClient(timeout=self._timeout_s) as client:
                response = await client.get(url, params=params)
        except httpx.TimeoutException as exc:
            raise ProviderError(
                self.name, "route", kind=ErrorCode.PROVIDER_TIMEOUT, message="OSRM 请求超时"
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(
                self.name,
                "route",
                kind=ErrorCode.PROVIDER_UNAVAILABLE,
                message=f"OSRM 请求失败：{type(exc).__name__}",
            ) from exc

        if response.status_code >= 400:
            raise ProviderError(
                self.name,
                "route",
                kind=ErrorCode.PROVIDER_UNAVAILABLE,
                message=f"OSRM 返回 HTTP {response.status_code}",
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise ProviderError(
                self.name,
                "route",
                kind=ErrorCode.PROVIDER_INVALID_RESPONSE,
                message="OSRM 返回了非 JSON 响应",
            ) from exc

        return _to_leg(body, mode=mode)


def _to_leg(body: object, *, mode: TransportMode) -> MapLeg:
    if not isinstance(body, dict) or body.get("code") != "Ok":
        raise ProviderError(
            "osrm",
            "route",
            kind=ErrorCode.PROVIDER_INVALID_RESPONSE,
            message=f"OSRM 未返回可用路线（code={body.get('code') if isinstance(body, dict) else '?'}）",
        )
    routes = body.get("routes")
    if not isinstance(routes, list) or not routes:
        raise ProviderError(
            "osrm", "route", kind=ErrorCode.PROVIDER_INVALID_RESPONSE, message="OSRM 响应缺少 routes"
        )
    first = routes[0]
    distance = first.get("distance") if isinstance(first, dict) else None
    if not isinstance(distance, int | float):
        raise ProviderError(
            "osrm", "route", kind=ErrorCode.PROVIDER_INVALID_RESPONSE, message="OSRM 响应缺少距离"
        )

    # 只有车行模式才认 duration：其余模式 OSRM 用 car profile 出的时间是错的
    duration_min: int | None = None
    duration_source: TransportSource | None = None
    if mode == "taxi":
        raw = first.get("duration")
        if isinstance(raw, int | float):
            # 向上取整，与高德以及 `domain.geo.travel_minutes` 保持同一政策：
            # 宁可多算 1 分钟，也不要四舍五入到让用户以为"来得及"
            # （89 秒 round 成 1 分钟，ceil 成 2 分钟；少算的那一分钟是用户的时间）。
            duration_min = max(1, math.ceil(raw / 60))
            duration_source = "osrm"
    return MapLeg(
        distance_m=round(distance),
        duration_min=duration_min,
        distance_source="osrm",
        duration_source=duration_source,
        mode=mode,
    )
