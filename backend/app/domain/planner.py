"""路线组合（**纯函数，零 IO**）。对应 PRD §11.5 的束搜索。

设计取舍：
    - 束搜索的"剪枝"只执行**可在局部判定**的硬约束（时间窗/步行/单段通勤上限），
      其余（预算/折返/闭馆）留在路径**完整后**由可行性校验统一判定。
    - 启发式评分用``候选分 + 关系分``的简单和，不调用完整 score_route（太贵）。
      完整评分只在最终筛选时做一次。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from app.core.config import LimitsConfig, ScoringConfig
from app.domain.budget import estimate_route_budget
from app.domain.candidates import Candidate, stay_duration_min
from app.domain.feasibility import validate_route, weekday_of
from app.domain.models import (
    ArchetypeName,
    Constraint,
    Intent,
    Place,
    RelationIndex,
    RoutePlan,
    Stop,
    WeatherCondition,
)
from app.domain.scoring import score_route
from app.domain.transit import leg_between

__all__ = ["effective_walking_cap", "plan_routes"]


@dataclass(frozen=True, slots=True)
class _BeamNode:
    path_ids: tuple[str, ...]
    stops: tuple[Stop, ...]
    total_walking: int = 0
    heuristic: float = 0.0

    @property
    def current_time(self) -> int:
        if not self.stops:
            return 0
        last = self.stops[-1]
        return last.depart_min + (last.leg_to_next.minutes if last.leg_to_next else 0)

    @property
    def last_place(self) -> Place:
        return self.stops[-1].place


def plan_routes(
    candidates: Sequence[Candidate],
    intent: Intent,
    constraints: Sequence[Constraint],
    *,
    relations: RelationIndex,
    limits: LimitsConfig,
    scoring: ScoringConfig,
    archetype: ArchetypeName,
    weather: WeatherCondition | None = None,
    river_centerline: Sequence[tuple[float, float]] | None = None,
) -> list[RoutePlan]:
    """为指定 archetype 生成路线方案。"""
    if len(candidates) < limits.planning.candidate_min:
        return []

    travel_modes = limits.travel_modes
    walking_cap = _effective_walking_cap(intent, constraints, limits)
    hard_walking_limit = int(walking_cap * scoring.formulas.walking_fit.hard_limit_ratio)
    min_stops = limits.planning.min_route_stops
    max_stops = scoring.archetypes[archetype].max_stops
    beam_width = limits.planning.beam_width
    max_depth = min(max_stops, limits.planning.max_depth)
    prune_min = limits.planning.single_leg_transit_prune_min
    end_min = intent.end_min
    weekday = weekday_of(intent.travel_date)

    candidate_by_id = {c.place.id: c for c in candidates}
    candidate_places = [c.place for c in candidates]
    neighbor_map = _build_neighbors(candidate_places, relations, limits.planning.neighborhood_radius_m)

    all_paths: list[_BeamNode] = []
    anchors = [c.place for c in candidates[: limits.planning.anchor_pool]]

    for anchor in anchors:
        start = Stop(place=anchor, arrive_min=intent.start_min, stay_min=stay_duration_min(anchor))
        root = _BeamNode(path_ids=(anchor.id,), stops=(start,), total_walking=0, heuristic=0.0)
        frontier = [root]
        for _depth in range(1, max_depth):
            next_frontier: list[_BeamNode] = []
            for node in frontier:
                for nxt_place in neighbor_map.get(node.last_place.id, ()):
                    if nxt_place.id in node.path_ids:
                        continue
                    if nxt_place.id not in candidate_by_id:
                        continue
                    leg = leg_between(node.last_place, nxt_place, relations, travel_modes)
                    if leg.minutes > prune_min:
                        continue
                    arrive = node.current_time + leg.minutes
                    if arrive > end_min:
                        continue
                    new_walking = node.total_walking + (leg.distance_m if leg.is_walk else 0)
                    if new_walking > hard_walking_limit:
                        continue

                    # 更新上一站的 leg_to_next，并追加新站
                    prev = node.stops[-1]
                    updated_prev = Stop(
                        place=prev.place,
                        arrive_min=prev.arrive_min,
                        stay_min=prev.stay_min,
                        leg_to_next=leg,
                    )
                    stay = stay_duration_min(nxt_place)
                    new_stop = Stop(
                        place=nxt_place,
                        arrive_min=arrive,
                        stay_min=stay,
                        leg_to_next=None,
                    )
                    new_stops = (*node.stops[:-1], updated_prev, new_stop)

                    rel_score = relations.score_between(node.last_place.id, nxt_place.id) or 0.5
                    h = node.heuristic + candidate_by_id[nxt_place.id].score + rel_score
                    next_frontier.append(
                        _BeamNode(
                            path_ids=(*node.path_ids, nxt_place.id),
                            stops=new_stops,
                            total_walking=new_walking,
                            heuristic=h,
                        )
                    )
            next_frontier.sort(key=lambda n: n.heuristic, reverse=True)
            frontier = next_frontier[:beam_width]
            all_paths.extend([n for n in frontier if len(n.path_ids) >= min_stops])

    if not all_paths:
        return []

    unique = _dedupe_by_jaccard(all_paths, max_similarity=limits.planning.max_route_similarity)

    plans: list[RoutePlan] = []
    for node in unique[: limits.planning.paths_per_archetype]:
        budget = estimate_route_budget(node.stops, intent, limits.budget)
        report = validate_route(
            node.stops,
            intent,
            limits=limits,
            scoring=scoring,
            budget=budget,
            walking_cap_m=walking_cap,
            weekday=weekday,
            river_centerline=river_centerline,
        )
        if not report.feasible:
            continue
        plans.append(RoutePlan(archetype=archetype, stops=node.stops, budget=budget))

    scored = []
    for plan in plans:
        breakdown = score_route(
            plan.stops,
            intent,
            scoring=scoring,
            archetype=archetype,
            relations=relations,
            budget=plan.budget,
            walking_cap_m=walking_cap,
            weather=weather,
        )
        scored.append((breakdown.total, plan, breakdown))
    scored.sort(key=lambda x: x[0], reverse=True)

    return [plan for _, plan, _ in scored[: limits.planning.max_output_routes]]


def effective_walking_cap(
    intent: Intent, constraints: Sequence[Constraint], limits: LimitsConfig
) -> int:
    """本次行程的步行上限（米）。

    显式的 ``max_walking_m`` 约束优先于节奏默认值。
    公开它（而不是让编排层从 ``_effective_walking_cap`` 复制一份）是为了让
    "组合时的剪枝"与"校验/评分时的步行上限"永远是**同一个数字** ——
    两处各算一次，迟早会在某次改配置后漂移。
    """
    for c in constraints:
        if c.type == "max_walking_m" and isinstance(c.value, (int, float)):
            return int(c.value)
    return limits.walking_caps_m.get(intent.pace, 6000)


#: 兼容旧名（模块内既有调用）。保留一个是非导入点，避免出现"两个实现"。
_effective_walking_cap = effective_walking_cap


def _build_neighbors(
    places: Sequence[Place],
    relations: RelationIndex,
    radius_m: int,
) -> dict[str, list[Place]]:
    from app.domain.geo import haversine_m

    result: dict[str, list[Place]] = {p.id: [] for p in places}
    for i, a in enumerate(places):
        for b in places[i + 1 :]:
            relation = relations.lookup(a.id, b.id)
            distance_m = relation.distance_m if relation and relation.distance_m is not None else int(
                haversine_m(a.lat, a.lng, b.lat, b.lng)
            )
            if distance_m <= radius_m:
                result[a.id].append(b)
                result[b.id].append(a)
    return result


def _dedupe_by_jaccard(paths: Sequence[_BeamNode], max_similarity: float) -> list[_BeamNode]:
    """按 Jaccard 相似度去重：地点集合重叠超过阈值则只保留启发分更高的。"""
    sets = [frozenset(p.path_ids) for p in paths]
    kept: list[_BeamNode] = []
    for path, ids in zip(paths, sets, strict=False):
        duplicate = False
        for existing in kept:
            union = len(ids | frozenset(existing.path_ids))
            if union == 0:
                continue
            sim = len(ids & frozenset(existing.path_ids)) / union
            if sim > max_similarity:
                duplicate = True
                break
        if not duplicate:
            kept.append(path)
    return kept
