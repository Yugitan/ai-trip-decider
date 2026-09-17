"""路线可行性校验（**纯函数、零副作用、零 IO**）。对应 PRD §12。

这是本项目的技术护城河，也是"不可执行路线"唯一的判定口径。三条铁律：

1. **硬约束判定绝不由 LLM 决定**（PRD §11.3 红线）。
2. **幂等且无副作用**：同样的输入必须给出同样的输出；不写库、不发请求。
3. **拿不到的数据就说不知道**：营业时间未知 → 报 HOURS_UNKNOWN；未指定出行日期 →
   报 HOURS_NOT_CHECKED，而不是假设"营业"。

校验码（PRD §12.2 的 16 项 + 2 项本模块扩展）：
    扩展项都写在 :data:`EXTENDED_CODES` 里，并注明为什么 PRD 清单里没有它。
"""

from __future__ import annotations

import itertools
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Literal

from app.core.config import LimitsConfig, ScoringConfig
from app.domain.budget import budget_cap_per_person, to_per_person
from app.domain.models import (
    REST_CATEGORIES,
    BudgetEstimate,
    Intent,
    RouteMetrics,
    Stop,
    minutes_to_hhmm,
    route_metrics,
)
from app.domain.scoring import reversal_indices

__all__ = [
    "EXTENDED_CODES",
    "FeasibilityReport",
    "Violation",
    "count_river_crossings",
    "validate_route",
    "weekday_of",
]

# PRD §12.2 之外的两个校验码：
#   - ESTIMATED_TRANSIT：PRD §23.2 第 31 条要求"通勤来源 estimated → 自动附加 warning"；
#   - HOURS_NOT_CHECKED：营业时间已知但用户没给出行日期时，既不能判"闭馆"，
#     也不该报"营业时间未知"（它是已知的）。
#
# 另外两个是**按天设置**（主题日）的交代，由规划层产生而不是 ``validate_route`` ——
# 那一天自己的站点是可行的，做不到的是"这一天的主题"：
#   - THEME_SHORTFALL：主题站点占当天的比例低于 ``limits.planning.theme_day.min_ratio``；
#   - THEME_IGNORED：那一天选了主题，但用户又在补充要求里说了不要这个方向。
EXTENDED_CODES: frozenset[str] = frozenset(
    {"ESTIMATED_TRANSIT", "HOURS_NOT_CHECKED", "THEME_IGNORED", "THEME_SHORTFALL"}
)

# 全部 18 项校验码，默认全开。注意不含配置本身没有的常量 —— 这个集合只用于
# ``enabled_checks`` 的默认值，实际行为由 ``allows(code)`` 守卫。
_ALL_CODES: frozenset[str] = frozenset({
    "BACKTRACK",
    "BUDGET_OVER_LIMIT",
    "CLOSED_AT_ARRIVAL",
    "DATA_CONFLICTING",
    "DETOUR",
    "DUPLICATE_PLACE",
    "ESTIMATED_TRANSIT",
    "HOURS_NOT_CHECKED",
    "HOURS_UNKNOWN",
    "IDLE_GAP",
    "LAST_ENTRY_MISSED",
    "LOW_DIVERSITY",
    "PRICE_UNKNOWN",
    "RIVER_CROSSING_REPEAT",
    "TIME_CONFLICT",
    "TRANSPORT_IMPOSSIBLE",
    "WALKING_OVER_LIMIT",
    "WINDOW_OVERFLOW",
})

Severity = Literal["hard", "soft"]


@dataclass(frozen=True, slots=True)
class Violation:
    code: str
    severity: Severity
    message: str
    at_seq: int | None = None
    detail: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class FeasibilityReport:
    """PRD §12.1 的报告结构。"""

    feasible: bool
    violations: tuple[Violation, ...]
    warnings: tuple[Violation, ...]
    metrics: RouteMetrics

    @property
    def hard_codes(self) -> tuple[str, ...]:
        return tuple(item.code for item in self.violations)

    def as_dict(self) -> dict[str, Any]:
        return {
            "feasible": self.feasible,
            "violations": [
                {"code": v.code, "severity": v.severity, "at_seq": v.at_seq,
                 "message": v.message, "detail": dict(v.detail)}
                for v in self.violations
            ],
            "warnings": [
                {"code": w.code, "severity": w.severity, "at_seq": w.at_seq,
                 "message": w.message, "detail": dict(w.detail)}
                for w in self.warnings
            ],
            "metrics": {
                "total_duration_min": self.metrics.total_duration_min,
                "walking_m": self.metrics.walking_m,
                "transit_min": self.metrics.transit_min,
                "place_count": self.metrics.place_count,
            },
        }


# ── 几何辅助：跨江判定 ──────────────────────────────────────────────────────


def _orientation(ax: float, ay: float, bx: float, by: float, cx: float, cy: float) -> float:
    return (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)


def _segments_cross(
    p1: tuple[float, float], p2: tuple[float, float], p3: tuple[float, float], p4: tuple[float, float]
) -> bool:
    """两线段是否相交（共线端点相接视为不相交）。"""
    d1 = _orientation(p3[0], p3[1], p4[0], p4[1], p1[0], p1[1])
    d2 = _orientation(p3[0], p3[1], p4[0], p4[1], p2[0], p2[1])
    d3 = _orientation(p1[0], p1[1], p2[0], p2[1], p3[0], p3[1])
    d4 = _orientation(p1[0], p1[1], p2[0], p2[1], p4[0], p4[1])
    return ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0))


def count_river_crossings(
    stops: Sequence[Stop], centerline: Sequence[tuple[float, float]]
) -> int:
    """统计**最长连续**跨江段数。``centerline`` 为江岸中心线（lat, lng）折线。"""
    if len(centerline) < 2 or len(stops) < 2:
        return 0

    best = streak = 0
    for current, nxt in itertools.pairwise(stops):
        if current.leg_to_next is None:
            continue
        start = (current.place.lat, current.place.lng)
        end = (nxt.place.lat, nxt.place.lng)
        crossed = any(
            _segments_cross(start, end, centerline[i], centerline[i + 1])
            for i in range(len(centerline) - 1)
        )
        streak = streak + 1 if crossed else 0
        best = max(best, streak)
    return best


# ── 主入口 ──────────────────────────────────────────────────────────────────


def validate_route(
    stops: Sequence[Stop],
    intent: Intent,
    *,
    limits: LimitsConfig,
    scoring: ScoringConfig,
    budget: BudgetEstimate | None = None,
    walking_cap_m: int | None = None,
    enabled_checks: frozenset[str] | None = None,
    river_centerline: Sequence[tuple[float, float]] | None = None,
    weekday: int | None = None,
) -> FeasibilityReport:
    """校验一条已排好时间的路线是否可执行。

    :param enabled_checks: 需要执行的校验码集合；``None`` 表示全开。
    :param weekday: 出行日期是周几（0=周一）。``None`` 表示未指定 ——
        此时不判定闭馆，改为报 HOURS_NOT_CHECKED。
    """
    checks = enabled_checks if enabled_checks is not None else _ALL_CODES
    metrics = route_metrics(stops)
    violations: list[Violation] = []
    warnings: list[Violation] = []

    def allows(code: str) -> bool:
        return code in checks

    # ── H4 重复地点 ──
    if allows("DUPLICATE_PLACE"):
        seen: dict[str, int] = {}
        for index, stop in enumerate(stops, start=1):
            if stop.place.id in seen:
                violations.append(
                    Violation(
                        code="DUPLICATE_PLACE",
                        severity="hard",
                        at_seq=index,
                        message=f"{stop.place.name} 在路线中重复出现（第 {seen[stop.place.id]} 站已去过）",
                        detail={"place_id": stop.place.id, "first_seq": seen[stop.place.id]},
                    )
                )
            else:
                seen[stop.place.id] = index

    # ── H5 折返（几何反向）──
    if allows("BACKTRACK"):
        for index in reversal_indices(stops):
            violations.append(
                Violation(
                    code="BACKTRACK",
                    severity="hard",
                    at_seq=index + 1,
                    message=f"第 {index}–{index + 2} 站出现折返（方向反转超过 135°）",
                )
            )

    # ── H2 时间窗 ──
    if allows("WINDOW_OVERFLOW"):
        if stops and metrics.start_min < intent.start_min:
            violations.append(
                Violation(
                    code="WINDOW_OVERFLOW",
                    severity="hard",
                    at_seq=1,
                    message=f"出发时刻早于可用时间窗（{minutes_to_hhmm(metrics.start_min)} < {minutes_to_hhmm(intent.start_min)}）",
                    detail={"start_min": metrics.start_min, "window_start_min": intent.start_min},
                )
            )
        if stops and metrics.end_min > intent.end_min:
            violations.append(
                Violation(
                    code="WINDOW_OVERFLOW",
                    severity="hard",
                    at_seq=len(stops),
                    message=f"结束时刻超出可用时间窗（{minutes_to_hhmm(metrics.end_min)} > {minutes_to_hhmm(intent.end_min)}）",
                    detail={"end_min": metrics.end_min, "window_end_min": intent.end_min},
                )
            )

    # ── H1 时间冲突 / H6 单段通勤 ──
    for index, (current, nxt) in enumerate(itertools.pairwise(stops), start=1):
        leg = current.leg_to_next
        if leg is None:
            continue
        earliest_arrival = current.depart_min + leg.minutes
        if allows("TIME_CONFLICT") and earliest_arrival > nxt.arrive_min:
            violations.append(
                Violation(
                    code="TIME_CONFLICT",
                    severity="hard",
                    at_seq=index + 1,
                    message=(
                        f"{minutes_to_hhmm(nxt.arrive_min)} 到达 {nxt.place.name}，"
                        f"但 {current.place.name}→{nxt.place.name} 实际需 {leg.minutes} 分钟"
                        f"（最早 {minutes_to_hhmm(earliest_arrival)} 才能到，来源：{leg.source}）"
                    ),
                    detail={
                        "arrive": minutes_to_hhmm(nxt.arrive_min),
                        "earliest_possible": minutes_to_hhmm(earliest_arrival),
                        "transport_source": leg.source,
                    },
                )
            )
        if allows("TRANSPORT_IMPOSSIBLE") and leg.minutes > limits.planning.single_leg_transit_prune_min:
            violations.append(
                Violation(
                    code="TRANSPORT_IMPOSSIBLE",
                    severity="hard",
                    at_seq=index + 1,
                    message=f"单段通勤 {leg.minutes} 分钟，超过上限 {limits.planning.single_leg_transit_prune_min} 分钟",
                    detail={"minutes": leg.minutes, "limit_min": limits.planning.single_leg_transit_prune_min},
                )
            )

    # ── H7 步行上限 ──
    if allows("WALKING_OVER_LIMIT") and walking_cap_m is not None and walking_cap_m > 0:
        hard_ratio = scoring.formulas.walking_fit.hard_limit_ratio
        hard_limit = walking_cap_m * hard_ratio
        if metrics.walking_m > hard_limit:
            violations.append(
                Violation(
                    code="WALKING_OVER_LIMIT",
                    severity="hard",
                    message=f"步行 {metrics.walking_m}m 超过上限 {int(hard_limit)}m（用户上限 {walking_cap_m}m × {hard_ratio}）",
                    detail={"walking_m": metrics.walking_m, "hard_limit_m": int(hard_limit)},
                )
            )

    # ── H8 预算上限 ──
    if allows("BUDGET_OVER_LIMIT") and budget is not None:
        cap = budget_cap_per_person(intent.budget, intent.people)
        if cap is not None:
            est = to_per_person(budget.max_cny, budget.scope, intent.people)
            limit = cap * Decimal(str(limits.budget.over_limit_ratio))
            if est > limit:
                violations.append(
                    Violation(
                        code="BUDGET_OVER_LIMIT",
                        severity="hard",
                        message=f"预计花费 ¥{est} 超过预算上限 ¥{limit}（人均，用户预算 ¥{cap} × {limits.budget.over_limit_ratio}）",
                        detail={"estimate_cny": str(est), "limit_cny": str(limit)},
                    )
                )

    # ── H3 营业时间 ──
    if allows("CLOSED_AT_ARRIVAL") or allows("LAST_ENTRY_MISSED") or allows("HOURS_UNKNOWN"):
        buffer = limits.planning.last_entry_buffer_min
        for index, stop in enumerate(stops, start=1):
            hours = stop.place.opening_hours
            if hours is None or hours.is_unknown:
                if allows("HOURS_UNKNOWN"):
                    detail: dict[str, Any] = {"place": stop.place.name}
                    if index == len(stops):
                        detail["last_position"] = True
                    warnings.append(
                        Violation(
                            code="HOURS_UNKNOWN",
                            severity="soft",
                            at_seq=index,
                            message=(
                                f"{stop.place.name} 营业时间未知，出发前请确认"
                                + ("（且排在末位，建议调整顺序）" if index == len(stops) else "")
                            ),
                            detail=detail,
                        )
                    )
                continue
            if weekday is None:
                if allows("HOURS_NOT_CHECKED"):
                    warnings.append(
                        Violation(
                            code="HOURS_NOT_CHECKED",
                            severity="soft",
                            at_seq=index,
                            message="未指定出行日期，无法校验营业时间（景点常按周几闭馆）",
                            detail={"place": stop.place.name},
                        )
                    )
                continue

            windows = hours.windows_at(weekday) or ()
            covering = next((w for w in windows if w.covers(stop.arrive_min)), None)
            if allows("CLOSED_AT_ARRIVAL") and covering is None:
                violations.append(
                    Violation(
                        code="CLOSED_AT_ARRIVAL",
                        severity="hard",
                        at_seq=index,
                        message=f"{minutes_to_hhmm(stop.arrive_min)} 到达 {stop.place.name} 时未营业",
                        detail={"arrive": minutes_to_hhmm(stop.arrive_min), "weekday": weekday},
                    )
                )
                continue
            if allows("LAST_ENTRY_MISSED") and covering is not None:
                last_entry = covering.last_entry_min or covering.close_min
                if stop.arrive_min + buffer > last_entry:
                    violations.append(
                        Violation(
                            code="LAST_ENTRY_MISSED",
                            severity="hard",
                            at_seq=index,
                            message=(
                                f"{minutes_to_hhmm(stop.arrive_min)} 到达 {stop.place.name}，"
                                f"距停止入场（{minutes_to_hhmm(last_entry)}）不足 {buffer} 分钟"
                            ),
                            detail={"arrive": minutes_to_hhmm(stop.arrive_min), "last_entry": minutes_to_hhmm(last_entry)},
                        )
                    )

    # ── 软约束 ──
    if allows("PRICE_UNKNOWN") and budget is not None and budget.unknown_items:
        warnings.append(
            Violation(
                code="PRICE_UNKNOWN",
                severity="soft",
                message=f"{len(budget.unknown_items)} 个地点缺少价格数据，预算为下限估算（未计入）",
                detail={"items": list(budget.unknown_items)},
            )
        )

    if allows("IDLE_GAP"):
        for index, (current, nxt) in enumerate(itertools.pairwise(stops), start=1):
            leg = current.leg_to_next
            if leg is None:
                continue
            gap = nxt.arrive_min - current.depart_min - leg.minutes
            if gap <= 30:
                continue
            if current.place.category in REST_CATEGORIES or nxt.place.category in REST_CATEGORIES:
                continue
            warnings.append(
                Violation(
                    code="IDLE_GAP",
                    severity="soft",
                    at_seq=index,
                    message=f"{current.place.name} → {nxt.place.name} 之间有 {gap} 分钟无意义等待",
                    detail={"idle_min": gap},
                )
            )

    if allows("DETOUR") and metrics.travel_ratio > _detour_threshold(scoring):
        warnings.append(
            Violation(
                code="DETOUR",
                severity="soft",
                message=f"通勤时间占比 {metrics.travel_ratio:.0%} 偏高，路线绕路较多",
                detail={"travel_ratio": round(metrics.travel_ratio, 4)},
            )
        )

    if allows("RIVER_CROSSING_REPEAT") and river_centerline:
        streak = count_river_crossings(stops, river_centerline)
        if streak >= 3:
            warnings.append(
                Violation(
                    code="RIVER_CROSSING_REPEAT",
                    severity="soft",
                    message=f"连续 {streak} 段跨越珠江，来回过江比较费时间",
                    detail={"consecutive_crossings": streak},
                )
            )

    if allows("DATA_CONFLICTING"):
        conflicting = [stop.place.name for stop in stops if stop.place.verification_status == "conflicting"]
        if conflicting:
            warnings.append(
                Violation(
                    code="DATA_CONFLICTING",
                    severity="soft",
                    message=f"以下地点的数据存在冲突，出发前请核实：{'、'.join(conflicting)}",
                    detail={"places": conflicting},
                )
            )

    if allows("LOW_DIVERSITY") and len(stops) >= 3:
        counts: dict[str, int] = {}
        for stop in stops:
            counts[stop.place.category] = counts.get(stop.place.category, 0) + 1
        dominant_ratio = max(counts.values()) / len(stops)
        if dominant_ratio >= 0.8:
            warnings.append(
                Violation(
                    code="LOW_DIVERSITY",
                    severity="soft",
                    message="路线类别单一，可能玩得比较单调",
                    detail={"dominant_ratio": round(dominant_ratio, 4)},
                )
            )

    if allows("ESTIMATED_TRANSIT"):
        estimated = [index + 1 for index, stop in enumerate(stops) if stop.leg_to_next and stop.leg_to_next.source == "estimated"]
        if estimated:
            warnings.append(
                Violation(
                    code="ESTIMATED_TRANSIT",
                    severity="soft",
                    message=f"第 {'、'.join(map(str, estimated))} 段的耗时为估算值（非实时路况）",
                    detail={"legs": estimated},
                )
            )

    return FeasibilityReport(
        feasible=not violations,
        violations=tuple(violations),
        warnings=tuple(warnings),
        metrics=metrics,
    )


def _detour_threshold(scoring: ScoringConfig) -> float:
    """绕路警告阈值。复用 scoring 的 ``good_travel_ratio`` × 2，避免两个标准漂移。"""
    return scoring.formulas.efficiency.good_travel_ratio * 2


def weekday_of(travel_date: str | None) -> int | None:
    """``"2026-09-14"`` → ``0``（周一）。未指定日期返回 ``None``。"""
    if not travel_date:
        return None
    return date.fromisoformat(travel_date).weekday()
