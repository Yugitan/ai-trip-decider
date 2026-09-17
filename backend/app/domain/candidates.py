"""候选地点生成（**纯函数，零 IO**）。对应 PRD §FR-04。

流程：硬过滤 → 偏好打分 → 截断到上限 → 锚点选择 → 邻域扩展。

硬过滤必须保证"不该出现的地点绝不进候选"；软条件（如 popularity）只影响排序，
不在这一步剪枝 —— 否则路线组合会因为没有足够多样的素材而反复落入局部最优。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from app.core.config import LimitsConfig, ScoringConfig, get_limits_config, get_seed_config
from app.domain.models import Constraint, Intent, Pace, Place, Stop
from app.domain.scoring import dimension_score

__all__ = [
    "Candidate",
    "CandidateSet",
    "matches_theme",
    "select_anchors",
    "select_candidates",
    "stay_duration_min",
    "theme_coverage",
]


@dataclass(frozen=True, slots=True)
class Candidate:
    place: Place
    score: float
    why: tuple[str, ...]  # 命中了哪些偏好维度（供结果页"为什么推荐"使用）


@dataclass(frozen=True, slots=True)
class CandidateSet:
    items: tuple[Candidate, ...]
    total_considered: int = 0
    relaxed_reasons: tuple[str, ...] = ()


def stay_duration_min(
    place: Place, pace: Pace = "balanced", *, limits: LimitsConfig | None = None
) -> int:
    """推荐停留时长：地点自身字段 > 类别基线 > 兜底 60 分钟，再按节奏缩放。

    为什么节奏必须进来：``pace`` 如果只改步行上限，同一题面选「轻松」和「紧凑」
    排出的**站点与停留完全一致**，只有步行距离不同 —— 那不是两种节奏，是同一趟
    行程换了个标签。慢节奏的真实含义是**待得久、走得少**。

    缩放后按 5 分钟取整（一个"87 分钟"的停留时间是假精度，读者会觉得是实测值），
    并不低于 ``planning.min_stop_duration_min``：再短就不值得为它排一个站。
    """
    if place.recommended_duration_min is not None and place.recommended_duration_min > 0:
        base = place.recommended_duration_min
    else:
        base = get_seed_config().duration_bases.get(place.category, 60)

    cfg = limits or get_limits_config()
    scale = cfg.planning.stay_scale_by_pace.get(pace, 1.0)
    scaled = round(base * scale / 5) * 5
    return max(cfg.planning.min_stop_duration_min, int(scaled))


def select_candidates(
    places: Sequence[Place],
    intent: Intent,
    constraints: Sequence[Constraint],
    *,
    scoring: ScoringConfig,
    limits: LimitsConfig,
) -> CandidateSet:
    """从全部地点中选出适合本次规划的候选集。

    硬过滤：
        - 状态非 active
        - 被排除（exclude_place）
        - 预算门票过贵（price_min > 预算 per_person）
        - 出行日期当天闭馆（有日期且营业时间已知时）
    排序：按 ``0.6·偏好匹配 + 0.4·热度``；截断到 candidate_max。
    不足 candidate_min 时逐步放宽次级约束并记录原因。
    """
    active_prefs = intent.active_preferences
    budget_cap = _budget_cap(intent)

    excluded_names = frozenset(
        c.value for c in constraints if c.type == "exclude_place" and isinstance(c.value, str)
    )
    excluded_ids = frozenset(
        c.value for c in constraints if c.type == "exclude_place_id" and isinstance(c.value, str)
    )

    def hard_ok(place: Place, *, relax_budget: bool = False, relax_hours: bool = False) -> bool:
        if not place.is_active:
            return False
        if place.id in excluded_ids or place.name in excluded_names:
            return False
        if (
            not relax_budget
            and budget_cap is not None
            and place.price_min is not None
            and place.price_min > budget_cap
        ):
            return False
        if not relax_hours and intent.travel_date and place.has_hours:
            from app.domain.feasibility import weekday_of

            wd = weekday_of(intent.travel_date)
            if wd is not None:
                windows = place.opening_hours.windows_at(wd) if place.opening_hours else None
                if windows == ():  # 明确闭馆（不是未知）
                    return False
        return True

    # ── 第一轮：全硬过滤 ──
    filtered = [p for p in places if hard_ok(p)]
    reasons: list[str] = []

    # ── 不足时放宽预算 ──
    if len(filtered) < limits.planning.candidate_min:
        filtered = [p for p in places if hard_ok(p, relax_budget=True)]
        if len(filtered) >= limits.planning.candidate_min:
            reasons.append("放宽了预算上限过滤（部分地点门票可能超预算）")

    # ── 仍不足时放宽营业时间 ──
    if len(filtered) < limits.planning.candidate_min:
        filtered = [p for p in places if hard_ok(p, relax_budget=True, relax_hours=True)]
        if len(filtered) >= limits.planning.candidate_min:
            reasons.append("放宽了闭馆日过滤（出行日期可能闭馆，出发前请确认）")

    scored = [_score_place(p, active_prefs, scoring) for p in filtered]
    scored.sort(key=lambda c: c.score, reverse=True)

    # ── 截断到上限 ──
    max_size = limits.planning.candidate_max
    kept = tuple(scored[:max_size])

    return CandidateSet(
        items=kept,
        total_considered=len(places),
        relaxed_reasons=tuple(reasons),
    )


def _budget_cap(intent: Intent) -> float | None:
    """人均预算上限；不限预算返回 None。"""
    if intent.budget.is_unlimited:
        return None
    assert intent.budget.amount is not None
    from decimal import Decimal

    from app.domain.budget import to_per_person

    return float(to_per_person(Decimal(str(intent.budget.amount)), intent.budget.scope, intent.people))


def _score_place(
    place: Place,
    active_prefs: Mapping[str, float],
    scoring: ScoringConfig,
) -> Candidate:
    """单个地点的候选分值（0..1）。"""
    unknown_default = scoring.formulas.preference.unknown_score_default
    weight_sum = sum(active_prefs.values())
    pref = 0.0
    why: list[str] = []
    if weight_sum > 0:
        for dim, weight in active_prefs.items():
            dimension = scoring.preference_dimensions.get(dim)
            if dimension is None:
                continue
            score = dimension_score(place, dim, dimension, unknown_default)
            pref += weight * score
            if score >= scoring.formulas.preference.coverage_min_dim_score:
                why.append(dim)
        pref /= weight_sum
    popularity = place.popularity_score if place.popularity_score is not None else unknown_default
    total = 0.6 * pref + 0.4 * popularity
    return Candidate(place=place, score=round(total, 6), why=tuple(why))


def matches_theme(place: Place, theme: str, scoring: ScoringConfig) -> bool:
    """这个地点算不算某个「主题日」的主题站点。

    判据与候选排序里的 ``why`` **完全一致**（维度分值 ≥ ``coverage_min_dim_score``）。
    同一件事只能有一个口径：否则会出现"卡片上写着「文化 0.90」，那一天却不把它
    算作文化站点"这种自相矛盾。

    分值缺失（``unknown_score_default`` = 0.35）**不算**命中主题 —— 不知道的地方
    不能被拿来充当"这一天的文化含量"。
    """
    dimension = scoring.preference_dimensions.get(theme)
    if dimension is None:
        return False
    score = dimension_score(
        place, theme, dimension, scoring.formulas.preference.unknown_score_default
    )
    return score >= scoring.formulas.preference.coverage_min_dim_score


def theme_coverage(
    stops: Sequence[Stop], theme: str, scoring: ScoringConfig
) -> tuple[int, int]:
    """``(主题站点数, 总站点数)``。空行程返回 ``(0, 0)``（而不是除零）。"""
    matched = sum(1 for stop in stops if matches_theme(stop.place, theme, scoring))
    return matched, len(stops)


def select_anchors(
    candidates: Sequence[Candidate],
    top_n: int,
) -> tuple[Place, ...]:
    """按 ``0.6·热度 + 0.4·偏好匹配`` 选锚点（多起点束搜索用）。

    注意这里用的是候选已经算好的 ``score``（它本身就是 0.6·pref + 0.4·pop），
    所以锚点公式等价于直接使用候选分。如果有模板路线命中，模板首站会被额外加入。
    """
    return tuple(c.place for c in candidates[:top_n])
