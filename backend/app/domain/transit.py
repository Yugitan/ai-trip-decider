"""两站之间"怎么去、要多久"的推导（**纯函数，零 IO**）。

★ 诚实性红线 ★
本模块产出的耗时有两种来源，必须区分且不可混淆：
    - ``osrm`` / ``amap``：来自真实路网服务（由 ``place_relations`` 表携带进来）；
    - ``estimated``：**由真实距离 × config/limits.yaml 里文档化的速度假设推导**。

为什么"时间"几乎总是 estimated（实测结论，写在这里避免后人重复踩坑）：
    公共 OSRM 演示实例（router.project-osrm.org）只部署了 car profile，
    请求 ``/route/v1/foot/...`` 返回的仍是车速。直接采用会得出"步行 3 分钟"的
    荒谬结论。所以只有**距离**可以信任真实路网，时间一律按文档化的速度假设换算
    （详见 app/domain/geo.py 顶部说明）。
"""

from __future__ import annotations

from collections.abc import Mapping

from app.core.config import TravelMode
from app.domain.geo import haversine_m, route_factor, travel_minutes
from app.domain.models import Leg, Place, Relation, RelationIndex, TransportMode

__all__ = [
    "crow_distance_m",
    "estimate_distance_m",
    "estimate_minutes",
    "leg_between",
    "recommend_mode",
]

# 地铁/公交的"是否真的存在"取决于站点数据。M1 的 B2 缺口：``transport_hub``
# 类目尚未抓取，因此**无法判断某两站之间是否有地铁可达**。
# 兜底策略：有真实路网距离就用它 + 地铁速度假设，否则用出租车速度假设
# （出租车在任何两点之间都可达，是"不会给出不可行方案"的保守选择）。
_WALK_PREFERRED_MAX_M = 1500
_TRANSIT_FALLBACK_MODE: TransportMode = "taxi"


def crow_distance_m(a: Place, b: Place) -> float:
    """两点直线距离（米）。"""
    return haversine_m(a.lat, a.lng, b.lat, b.lng)


def estimate_distance_m(a: Place, b: Place) -> int:
    """估算路网距离：直线距离 × 分档绕行系数（保守偏长，绝不比实际更短）。"""
    return round(crow_distance_m(a, b) * route_factor(crow_distance_m(a, b)))


def estimate_minutes(
    distance_m: int, mode: TransportMode, travel_modes: Mapping[str, TravelMode]
) -> int:
    """按 config/limits.yaml 的速度假设推导耗时（永远标记 estimated）。"""
    assumption = travel_modes.get(mode)
    if assumption is None:
        raise KeyError(
            f"config/limits.yaml 的 travel_modes 缺少 {mode!r} 的速度假设。"
            "拒绝用猜测的速度算时间 —— 那会让可行性校验失去意义。"
        )
    return travel_minutes(distance_m, assumption.speed_kmh, overhead_min=assumption.overhead_min)


def recommend_mode(distance_m: int, relation: Relation | None) -> TransportMode:
    """选择出行方式。

    - ≤ 1.5km：步行（广州老城区这个距离内步行通常比等地铁快，且不产生费用）
    - 更远：若关系表里给出了 transit 耗时 → 地铁；否则回退出租车。
      回退成出租车而不是地铁，是因为在缺少站点数据时假设"有地铁"会给出
      **走不通**的方案，而出租车的假设只会让方案保守一点。
    """
    if distance_m <= _WALK_PREFERRED_MAX_M:
        return "walk"
    if relation is not None and relation.minutes_for("metro") is not None:
        return "metro"
    return _TRANSIT_FALLBACK_MODE


def leg_between(
    a: Place,
    b: Place,
    relations: RelationIndex,
    travel_modes: Mapping[str, TravelMode],
    *,
    mode: TransportMode | None = None,
) -> Leg:
    """构造 a→b 的一段通勤。

    优先级（每一步都会如实反映到 ``source`` 上）：
    1. 关系表里有该出行方式的**实测/已算**耗时 → 用它，来源沿用关系表的 ``source``；
       但如果该方式在 ``derived_modes`` 里（耗时是距离推导出来的），来源降级为 estimated。
    2. 关系表里有距离但没有该方式的耗时 → 真实距离 × 速度假设 → estimated。
    3. 连关系都没有 → 直线距离 × 绕行系数 → estimated。
    """
    relation = relations.lookup(a.id, b.id)
    chosen = mode or recommend_mode(
        relation.distance_m if relation and relation.distance_m is not None else estimate_distance_m(a, b),
        relation,
    )

    if relation is not None:
        minutes = relation.minutes_for(chosen)
        distance_m = relation.distance_m
        if minutes is not None and distance_m is not None:
            source = "estimated" if chosen in relation.derived_modes else relation.source
            return Leg(mode=chosen, minutes=minutes, distance_m=distance_m, source=source)
        if distance_m is not None:
            return Leg(
                mode=chosen,
                minutes=estimate_minutes(distance_m, chosen, travel_modes),
                distance_m=distance_m,
                source="estimated",
            )

    distance_m = estimate_distance_m(a, b)
    return Leg(
        mode=chosen,
        minutes=estimate_minutes(distance_m, chosen, travel_modes),
        distance_m=distance_m,
        source="estimated",
    )
