"""地理计算（**纯函数，零 IO**）。

★ 关于"距离与时间"的诚实性约定（由实测得出，非常重要）★

实测结论（2026-09-10，对 https://router.project-osrm.org 直接探测）：
    请求 `/route/v1/foot/...` 与 `/route/v1/driving/...` 返回**完全相同**的结果
    （同一对坐标：distance=2349m, duration=189.6s → 44.6 km/h）。
    也就是说**公共 OSRM 演示实例只部署了 car profile**，它不会因为你在 URL 里写
    `foot` 就给你步行时间。

因此本模块把两类东西严格分开：
    1. **距离**：可以用 OSRM 的真实路网距离（`route_distance_m`），来源记 `osrm`。
    2. **时间**：只有在确实拿到了对应出行方式的真实耗时时才记 `osrm`；
       否则由**真实距离 × 文档化的速度假设**推导，来源必须记 `estimated`，
       且速度假设集中放在 config/limits.yaml 的 `travel_modes` 里，可审计、可调整。

绝不允许把车速当步行时间写进路线 —— 那会让用户看到"步行 3 分钟"实际要走 40 分钟。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from math import asin, ceil, cos, radians, sin, sqrt
from typing import Any

__all__ = [
    "EARTH_RADIUS_M",
    "CityBoundary",
    "bbox_contains",
    "haversine_m",
    "point_in_polygon",
    "point_in_rings",
    "route_factor",
    "travel_minutes",
]

EARTH_RADIUS_M = 6_371_008.8

# 直线距离 → 路网距离的经验绕行系数。仅在完全没有路网数据时使用，
# 用于给出**保守**（偏长）的估算，避免把距离算得比实际更短。
#
# 注意：实际生效的系数由 :func:`route_factor` 按距离分档返回（1.15 / 1.30 / 1.40），
# 这个常量目前**没有任何调用方**，保留它是为了给"统一兜底系数"留一个显式入口。
# 若 M2 决定不再需要它，应连同 :func:`route_factor` 一起评估后再删除。
DEFAULT_ROUTE_FACTOR = 1.30

# 耗时的取整精度：先把分钟数四舍五入到 1e-6 再向上取整，
# 避免 "0.3km / 4.5kmh * 60" 这类计算产生的 4.000000000000001 被当成"超过 4 分钟"。
_MINUTE_PRECISION = 6


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """两点间大圆距离（米）。"""
    phi1, phi2 = radians(lat1), radians(lat2)
    d_phi = phi2 - phi1
    d_lambda = radians(lng2 - lng1)
    a = sin(d_phi / 2) ** 2 + cos(phi1) * cos(phi2) * sin(d_lambda / 2) ** 2
    return 2 * EARTH_RADIUS_M * asin(sqrt(a))


def route_factor(crow_distance_m: float) -> float:
    """按直线距离给出绕行系数：短距离步行绕行更多，长距离更接近直线。"""
    if crow_distance_m < 300:
        return 1.15
    if crow_distance_m < 1500:
        return 1.30
    return 1.40


def bbox_contains(
    lat: float, lng: float, bbox: dict[str, float]
) -> bool:
    """点是否落在矩形 bbox 内（含边界）。

    注意：这是**粗筛**，不是城市归属判据 —— 矩形会切进邻市（见模块末尾关于
    「矩形 bbox 切进邻市」的说明）。判定"在不在本市"必须用 :func:`point_in_rings`。
    当前没有调用方（建库流程内联了同样的比较），保留它是为了让粗筛逻辑有唯一的、
    可测试的入口，而不是散落在脚本里。
    """
    return (
        bbox["min_lat"] <= lat <= bbox["max_lat"] and bbox["min_lng"] <= lng <= bbox["max_lng"]
    )


def travel_minutes(
    distance_m: float, speed_kmh: float, *, overhead_min: float = 0.0
) -> int:
    """由距离与速度推导耗时（分钟，向上取整到整数分钟）。

    ``overhead_min`` 表示固定开销（进出站、候车、找车位等）。
    调用方必须在写库时把结果标记为 ``estimated``，除非耗时来自真实路网服务。

    取整用「先按 1e-6 精度规整、再向上取整」，而不是 ``int(minutes + 0.999)``：
    后者在分钟小数部分落在 (0, 0.001] 时会**少算 1 分钟**（例如 4.0005 分钟
    会被算成 4 而不是 5），而这个函数是路线时长的来源，少算会直接让用户
    看到"比实际更短的行程"。先规整再 ceil 同时避开了另一个坑：
    浮点误差产生的 4.000000000000001 不该被算成 5 分钟。
    """
    if distance_m <= 0:
        return max(1, round(overhead_min))
    minutes = distance_m / 1000.0 / speed_kmh * 60.0 + overhead_min
    return max(1, ceil(round(minutes, _MINUTE_PRECISION)))


# ── 城市归属判定（点面判定）────────────────────────────────────────────────
#
# 为什么需要它（真实 bug）：整城 POI 抓取用的是**矩形 bbox**，而城市形状不规则，
# 矩形四角会切进邻市 —— 实测让「深圳野生动物园」「锦绣中华民俗村」（均在深圳）
# 进了广州知识库。地址标签与名称过滤能拦下大部分，但对"既无地址、名称也不含
# 城市名"的地点无能为力，只有行政边界本身是可靠判据。
#
# 平面近似说明：把经纬度当平面直角坐标做射线法，在城市尺度（几十公里）下
# 的误差远小于数据本身的精度（6 位小数约 0.1 米级），且不涉及跨越大范围的比较，
# 因此无需投影变换。这是刻意选择，不是疏忽。


@dataclass(frozen=True, slots=True)
class CityBoundary:
    """城市行政边界：外环取并集、内环（飞地/洞）取差集。"""

    outer: tuple[tuple[tuple[float, float], ...], ...]
    inner: tuple[tuple[tuple[float, float], ...], ...] = ()

    def __post_init__(self) -> None:
        if not self.outer:
            raise ValueError("CityBoundary 至少需要一个外环，否则会把所有地点判为城市外")

    @property
    def point_count(self) -> int:
        return sum(len(ring) for ring in self.outer) + sum(len(ring) for ring in self.inner)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> CityBoundary:
        def to_rings(key: str) -> tuple[tuple[tuple[float, float], ...], ...]:
            return tuple(
                tuple((float(point[0]), float(point[1])) for point in ring)
                for ring in (payload.get(key) or [])
            )

        return cls(outer=to_rings("outer"), inner=to_rings("inner"))


def point_in_polygon(lat: float, lng: float, ring: Sequence[tuple[float, float]]) -> bool:
    """射线法判断点是否在单个多边形内。

    ★ 边界点的确切语义（曾经的文档与实现不符，这里写清楚）★
    保证「顶点命中」：点与任一顶点坐标完全相同 → 返回 True。
    **不保证**「边上命中」：点落在某条边的中间（非顶点）时，结果完全由射线法决定，
    可能返回 False（例如落在水平边上时，该边按"不算穿越"处理）。
    这是刻意的取舍 —— 坐标来自 OSM（6 位小数，约 0.1 米级），
    恰好落在边线上的概率可忽略；而给"落在边上"加容差会引入一个任意的 epsilon，
    让城市边界判定变得不可复现。真正需要"边界算哪一边"时，应由调用方显式决定。

    :param ring: 顶点序列，``(lat, lng)``；首尾点重复的闭合环会被自动去重。
    """
    if len(ring) < 3:
        return False
    inside = False
    # 首尾点相同的闭合环会让最后一条边退化，提前去掉重复的收尾点
    points = ring[:-1] if ring[0] == ring[-1] else ring
    count = len(points)
    for i in range(count):
        y1, x1 = points[i]
        y2, x2 = points[(i + 1) % count]
        # 快速命中边界的情况：点正好落在水平边上
        if (y1 == lat and x1 == lng) or (y2 == lat and x2 == lng):
            return True
        if (y1 > lat) != (y2 > lat):
            # 交点横坐标
            x_at = x1 + (lat - y1) / (y2 - y1) * (x2 - x1)
            if x_at > lng:
                inside = not inside
    return inside


def point_in_rings(lat: float, lng: float, boundary: CityBoundary) -> bool:
    """点是否在行政边界内：任一外环命中且不在任何内环（洞）里。"""
    if not any(point_in_polygon(lat, lng, ring) for ring in boundary.outer):
        return False
    return not any(point_in_polygon(lat, lng, ring) for ring in boundary.inner)
