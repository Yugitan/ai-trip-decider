"""离线估算地图 Provider：真实路网拿不到时的兜底，**零网络、零成本**。

距离 = 大圆距离 × 分档绕行系数；耗时 = 距离 × ``config/limits.yaml`` 的速度假设。
两者来源一律 ``estimated``，UI 必须显示"估算"标签（PRD FR-05 AC-5.3）。
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal

from app.core.config import TravelMode
from app.domain.geo import haversine_m, route_factor, travel_minutes
from app.domain.models import TransportMode
from app.providers.base import LatLng, ProviderHealth
from app.providers.map.base import MapLeg

__all__ = ["HaversineMapProvider"]


class HaversineMapProvider:
    name = "haversine"

    def __init__(self, travel_modes: Mapping[str, TravelMode]) -> None:
        self._travel_modes = travel_modes

    def health(self) -> ProviderHealth:
        return ProviderHealth(
            name=self.name,
            available=False,
            detail="离线估算（距离/耗时为估算值，非实测）",
            degraded=True,
        )

    async def leg(self, origin: LatLng, destination: LatLng, *, mode: TransportMode) -> MapLeg:
        crow = haversine_m(origin.lat, origin.lng, destination.lat, destination.lng)
        distance_m = round(crow * route_factor(crow))
        assumption = self._travel_modes.get(mode)
        if assumption is None:
            # 没有该出行方式的速度假设 ⇒ 只给距离，不给耗时（不猜）
            return MapLeg(
                distance_m=distance_m,
                duration_min=None,
                distance_source="estimated",
                duration_source=None,
                mode=mode,
            )
        minutes = travel_minutes(distance_m, assumption.speed_kmh, overhead_min=assumption.overhead_min)
        return MapLeg(
            distance_m=distance_m,
            duration_min=minutes,
            distance_source="estimated",
            duration_source="estimated",
            mode=mode,
        )

    def estimate_cost(self, op: str, units: int = 1) -> Decimal:
        return Decimal("0")
