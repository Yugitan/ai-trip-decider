"""地图 Provider 契约（PRD §11.5、§13）。

★ 为什么距离与耗时分别带来源 ★
实测（见 ``app/domain/geo.py`` 顶部）：公共 OSRM 演示实例只有 car profile，
请求 ``foot`` 也返回车速。因此"距离真实、耗时是推导"是常态，
一个 ``source`` 字段表达不了这件事。这里拆成 ``distance_source`` 与
``duration_source``，让"哪些数字是真的"在类型层面就无处隐藏。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol, runtime_checkable

from app.domain.models import TransportMode, TransportSource
from app.providers.base import LatLng, ProviderHealth

__all__ = ["MapLeg", "MapProvider"]


@dataclass(frozen=True, slots=True)
class MapLeg:
    """两点之间一段通勤。

    ``duration_min=None`` 表示**该出行方式没有可信耗时** —— 调用方必须用
    ``config/limits.yaml`` 的速度假设推导并标记 estimated，绝不当成 0 分钟。
    """

    distance_m: int | None
    duration_min: int | None
    distance_source: TransportSource
    duration_source: TransportSource | None = None
    mode: TransportMode = "walk"


@runtime_checkable
class MapProvider(Protocol):
    name: str

    async def leg(self, origin: LatLng, destination: LatLng, *, mode: TransportMode) -> MapLeg | None:
        """返回一段通勤。``None`` 表示该模式不受支持（调用方需降级）。"""
        ...

    def health(self) -> ProviderHealth: ...

    def estimate_cost(self, op: str, units: int = 1) -> Decimal: ...
