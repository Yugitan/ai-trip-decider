"""路线评分（**纯函数，零 IO**）。对应 PRD §11。

公式（PRD §11.1）：
    FinalScore = [ Σ_i w_i · f_i ] × M_diversity × M_weather × M_conflict

三条必须守住的性质：
1. **权重只来自 config/scoring.yaml**。这里不写任何 0.30 / 0.20 之类的数字，
   也不允许 LLM 参与打分（PRD §11：LLM 只负责排序与叙事）。
2. **每个分项 ∈ [0,1]**，最终分 clamp 到 [0,1] —— 数据库的
   ``ck_trip_routes_score`` 要求 recommend_score 落在 0..1，乘数项（最高 1.1）
   有可能把加权和推到 1 以上，不 clamp 会直接被数据库拒绝。
3. **缺失数据用保守默认值，不用 0**。"不知道这个地点的美食分"不等于
   "它美食分为 0"；用 0 会让未知地点在排序里被系统性地低估到和"确实很差"同档。
"""

from __future__ import annotations

import itertools
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from math import cos, radians, sqrt

from app.core.config import Formulas, PreferenceDimension, ScoringConfig, Weights
from app.domain.budget import budget_cap_per_person
from app.domain.models import (
    REST_CATEGORIES,
    ArchetypeName,
    BudgetEstimate,
    Intent,
    Place,
    RelationIndex,
    RouteMetrics,
    Stop,
    WeatherCondition,
    route_metrics,
)

__all__ = [
    "ScoreBreakdown",
    "budget_fit",
    "clamp01",
    "conflict_multiplier",
    "count_reversals",
    "dimension_score",
    "diversity_multiplier",
    "place_relation_score",
    "popularity_score",
    "preference_score",
    "route_efficiency",
    "score_route",
    "time_fit",
    "walking_fit",
    "weather_multiplier",
]

# 判定"方向反转"的角度阈值：连续两段夹角超过 135° 才算折返。
# 用 135° 而不是 180° 是因为真实路网里"原路返回"不可能精确到 180°，
# 而 90° 转弯（老城区的常见走法）不应该被当成折返。
_REVERSAL_COS_THRESHOLD = -0.7071067811865476  # cos(135°)
# 过短的段不参与折返判定：两个 POI 相距几十米时方向向量基本是噪声
_MIN_REVERSAL_LEG_M = 50.0


def clamp01(value: float) -> float:
    return 0.0 if value < 0.0 else 1.0 if value > 1.0 else value


@dataclass(frozen=True, slots=True)
class ScoreBreakdown:
    """一次评分的全部中间结果。

    PRD §11.7 要求"评分透明"：前端的"为什么推荐这套"面板直接读这里，
    所以**每一项都必须存**，不能只留一个总分。
    """

    preference: float
    efficiency: float
    time_fit: float
    popularity: float
    budget_fit: float
    walking_fit: float
    place_relation: float
    m_diversity: float
    m_weather: float
    m_conflict: float
    weighted_sum: float
    total: float

    @property
    def recommend_score(self) -> float:
        """对外展示的推荐指数（0..1，两位小数）。"""
        return round(self.total, 2)

    def as_dict(self) -> dict[str, float]:
        return {
            "preference": self.preference,
            "efficiency": self.efficiency,
            "time_fit": self.time_fit,
            "popularity": self.popularity,
            "budget_fit": self.budget_fit,
            "walking_fit": self.walking_fit,
            "place_relation": self.place_relation,
            "m_diversity": self.m_diversity,
            "m_weather": self.m_weather,
            "m_conflict": self.m_conflict,
            "weighted_sum": self.weighted_sum,
            "total": self.total,
            "recommend_score": self.recommend_score,
        }


def _weighted_mean(values: Sequence[tuple[float, int]]) -> float:
    """按时长加权平均。总时长为 0 时退化为算术平均（空序列返回 0）。"""
    if not values:
        return 0.0
    total_weight = sum(weight for _, weight in values)
    if total_weight <= 0:
        return sum(value for value, _ in values) / len(values)
    return sum(value * weight for value, weight in values) / total_weight


# ──（1）偏好匹配度 ──────────────────────────────────────────────────────────


def dimension_score(
    place: Place,
    dim_key: str,
    dimension: PreferenceDimension,
    unknown_default: float,
) -> float:
    """某个地点在单个偏好维度上的分值（0..1）。

    取值顺序：
    1. 地点自带的分值（``Place.scores`` 用**偏好维度的键**存，如 ``food``；
       从数据库列名 ``food_score`` 的映射由上层完成）；
    2. 该维度若定义为"类别匹配"（配置里 ``field`` 为 null），按类别命中给 0/1；
    3. 都没有 → ``unknown_default``（**不是 0**）。
    """
    raw = place.scores.get(dim_key)
    if raw is not None:
        return clamp01(raw)
    if dimension.categories:
        return 1.0 if place.category in dimension.categories else 0.0
    return unknown_default


def preference_score(
    stops: Sequence[Stop],
    intent: Intent,
    dimensions: Mapping[str, PreferenceDimension],
    formulas: Formulas,
) -> float:
    """PRD §11.2（1）：0.65·时长加权均值 + 0.35·覆盖度。

    覆盖度存在的意义：防止"整条路线只有一个高分点把均值拉高"
    —— 也就是防止"美食路线其实只有一家餐厅"。
    """
    active = intent.active_preferences
    if not active:
        # 没有任何偏好维度时这一项没有定义。取中性值 0.5 而不是 0，
        # 是为了不让"用户没选偏好"被解释成"这条路线完全不符合偏好"。
        return 0.5

    cfg = formulas.preference
    weight_sum = sum(active.values())
    per_stop: list[tuple[float, int]] = []
    satisfied: dict[str, int] = dict.fromkeys(active, 0)

    for stop in stops:
        total = 0.0
        for dim, weight in active.items():
            dimension = dimensions.get(dim)
            if dimension is None:
                continue  # 用户选了配置里不存在的维度 → 该维度不参与（不猜）
            score = dimension_score(stop.place, dim, dimension, cfg.unknown_score_default)
            total += weight * score
            if score >= cfg.coverage_min_dim_score:
                satisfied[dim] += 1
        per_stop.append((total / weight_sum, max(1, stop.stay_min)))

    mean_pref = _weighted_mean(per_stop)
    covered = sum(1 for dim, count in satisfied.items() if count >= cfg.coverage_min_stops)
    coverage = covered / len(active)
    return clamp01(cfg.mean_weight * mean_pref + cfg.coverage_weight * coverage)


# ──（2）路线效率 ────────────────────────────────────────────────────────────


def _direction_vectors(stops: Sequence[Stop]) -> list[tuple[float, float]]:
    """相邻站点之间的方向向量（经度已按纬度做平面近似校正）。"""
    vectors: list[tuple[float, float]] = []
    for current, nxt in itertools.pairwise(stops):
        lat_mid = radians((current.place.lat + nxt.place.lat) / 2)
        dx = (nxt.place.lng - current.place.lng) * cos(lat_mid)
        dy = nxt.place.lat - current.place.lat
        vectors.append((dx, dy))
    return vectors


def reversal_indices(stops: Sequence[Stop]) -> list[int]:
    """返回发生折返的中间站点下标（第 i 段与第 i+1 段夹角 > 135°）。

    用平面近似算方向向量（城市尺度下误差远小于数据精度，见 app/domain/geo.py 说明）。
    过短的段（<50m）跳过：相邻 POI 之间的方向基本是噪声。
    """
    vectors = _direction_vectors(stops)
    indices: list[int] = []
    for index, (first, second) in enumerate(itertools.pairwise(vectors)):
        if _length(first) < _MIN_REVERSAL_LEG_M or _length(second) < _MIN_REVERSAL_LEG_M:
            continue
        if _cosine(first, second) < _REVERSAL_COS_THRESHOLD:
            indices.append(index)
    return indices


def count_reversals(stops: Sequence[Stop]) -> int:
    """折返次数（供 RouteEfficiency 的折返惩罚使用）。"""
    return len(reversal_indices(stops))


_METERS_PER_DEGREE = 111_320.0  # 1° ≈ 111.32km（城市尺度下的平面近似）


def _length(vector: tuple[float, float]) -> float:
    """方向向量的长度（米）。"""
    return sqrt(vector[0] ** 2 + vector[1] ** 2) * _METERS_PER_DEGREE


def _cosine(a: tuple[float, float], b: tuple[float, float]) -> float:
    """两个方向向量夹角的余弦。零向量（两点重合）视为同向，不记为折返。"""
    norm_a = sqrt(a[0] ** 2 + a[1] ** 2)
    norm_b = sqrt(b[0] ** 2 + b[1] ** 2)
    if norm_a == 0 or norm_b == 0:
        return 1.0
    return (a[0] * b[0] + a[1] * b[1]) / (norm_a * norm_b)


def route_efficiency(
    metrics: RouteMetrics,
    stops: Sequence[Stop],
    formulas: Formulas,
) -> float:
    """PRD §11.2（2）：通勤占比越低越好，折返额外扣分。"""
    cfg = formulas.efficiency
    ratio = metrics.travel_ratio
    span = cfg.bad_travel_ratio - cfg.good_travel_ratio
    base = clamp01(1 - (ratio - cfg.good_travel_ratio) / span) if span > 0 else 1.0
    penalty = min(cfg.reversal_penalty_cap, cfg.reversal_penalty * count_reversals(stops))
    return clamp01(base - penalty)


# ──（3）时间安排合理性 ──────────────────────────────────────────────────────


def time_fit(stops: Sequence[Stop], formulas: Formulas) -> float:
    """PRD §11.2（3）：匆忙（停留不足）与空耗（无意义等待）都要扣分。"""
    cfg = formulas.time_fit
    rush = 0.0
    for stop in stops:
        recommended = stop.place.recommended_duration_min
        if not recommended or recommended <= 0:
            continue
        if stop.stay_min < cfg.rush_ratio * recommended:
            rush += cfg.rush_penalty_per_stop * (1 - stop.stay_min / recommended)
    rush = min(cfg.rush_penalty_cap, rush)

    idle = 0.0
    for current, nxt in itertools.pairwise(stops):
        leg = current.leg_to_next
        if leg is None:
            continue
        gap = nxt.arrive_min - current.depart_min - leg.minutes
        if gap <= cfg.idle_threshold_min:
            continue
        # 吃饭/喝咖啡前后的等待不算空耗 —— 那正是"休息"的语义
        if current.place.category in REST_CATEGORIES or nxt.place.category in REST_CATEGORIES:
            continue
        idle += cfg.idle_penalty_per_step * (gap - cfg.idle_threshold_min) / cfg.idle_threshold_min
    idle = min(cfg.idle_penalty_cap, idle)

    return clamp01(1 - rush - idle)


# ──（4）热度 ────────────────────────────────────────────────────────────────


def popularity_score(
    stops: Sequence[Stop],
    formulas: Formulas,
    unknown_default: float,
) -> float:
    """PRD §11.2（4）：0.7·时长加权均值 + 0.3·最大值。

    保留 max 项是为了确保路线至少有一个"值得专程去"的亮点 ——
    全是 0.6 的平庸路线不该和"一个 0.95 + 几个 0.5"的路线同分。
    """
    cfg = formulas.popularity
    values = [
        (stop.place.popularity_score if stop.place.popularity_score is not None else unknown_default,
         max(1, stop.stay_min))
        for stop in stops
    ]
    if not values:
        return 0.0
    mean = _weighted_mean(values)
    best = max(value for value, _ in values)
    return clamp01(cfg.mean_weight * mean + cfg.max_weight * best)


# ──（5）预算贴合 ────────────────────────────────────────────────────────────


def budget_fit(
    estimate: BudgetEstimate | None,
    cap_per_person: Decimal | None,
    formulas: Formulas,
) -> float:
    """PRD §11.2（5）：不超预算时越接近预算越好（但不能浪费），超支线性扣到 0。

    ``estimate`` 为 None（没有价格数据）或 ``cap`` 为 None（用户不限预算）→ 1.0。
    注意"没有数据"不能记 0 分 —— 那等于因为知识库缺失而惩罚一条好路线。
    """
    if estimate is None or cap_per_person is None or cap_per_person <= 0:
        return 1.0
    cfg = formulas.budget_fit
    est = float(estimate.max_cny)
    cap = float(cap_per_person)
    over = max(0.0, est - cap)
    if over > 0:
        return clamp01(1 - over / (cfg.over_zero_point * cap))
    under_ratio = clamp01((cap - est) / cap)
    return clamp01(cfg.under_floor + cfg.under_bonus * min(1.0, under_ratio / cfg.under_bonus_ratio))


# ──（6）步行承受度 ──────────────────────────────────────────────────────────


def walking_fit(walking_m: int, cap_m: int | None) -> float:
    """PRD §11.2（6）：``1 − walking/cap``，达到上限即 0 分。

    ``cap_m`` 为 None（用户没有上限且没有节奏对应的默认值）→ 1.0。
    """
    if cap_m is None or cap_m <= 0:
        return 1.0
    return clamp01(1 - walking_m / cap_m)


# ──（7）地点组合质量 ────────────────────────────────────────────────────────


def place_relation_score(stops: Sequence[Stop], relations: RelationIndex) -> float:
    """PRD §11.2（7）：相邻两站 relationship_score 的均值，缺失关系默认 0.5。"""
    scores: list[float] = []
    for current, nxt in itertools.pairwise(stops):
        score = relations.score_between(current.place.id, nxt.place.id)
        scores.append(score if score is not None else 0.5)
    if not scores:
        return 0.5
    return clamp01(sum(scores) / len(scores))


# ── 乘数项 ──────────────────────────────────────────────────────────────────


def diversity_multiplier(stops: Sequence[Stop], formulas: Formulas) -> float:
    """PRD §11.2：类别越多样，乘数越高（1.0 ~ 1.1）。"""
    cfg = formulas.multipliers.diversity
    if not cfg.enabled or len(stops) < 2:
        return 1.0
    counts: dict[str, int] = {}
    for stop in stops:
        counts[stop.place.category] = counts.get(stop.place.category, 0) + 1
    dominant = max(counts.values()) / len(stops)
    return 1.0 + cfg.max_bonus * (1 - dominant)


def weather_multiplier(
    stops: Sequence[Stop],
    weather: WeatherCondition | None,
    formulas: Formulas,
) -> float:
    """PRD §11.2：雨天偏好室内/雨天的地点，高温偏好室内。无天气数据时恒为 1.0。

    ★ 诚实性 ★ 没有天气数据就不做任何天气调整（返回 1.0），
    而不是"猜一个常见天气"。
    """
    if weather is None or weather == "clear" or not stops:
        return 1.0
    cfg = formulas.multipliers.weather
    fits: list[float] = []
    for stop in stops:
        if weather == "rain":
            fits.append(
                stop.place.rainy_day_score
                if stop.place.rainy_day_score is not None
                else (1.0 if stop.place.indoor else 0.0)
            )
        else:  # heat
            fits.append(1.0 if stop.place.indoor else 0.0)
    fit = sum(fits) / len(fits)
    bonus = cfg.rain_bonus if weather == "rain" else cfg.heat_bonus
    return 1.0 + bonus * fit


def conflict_multiplier(stops: Sequence[Stop], formulas: Formulas) -> float:
    """PRD §11.2：含 conflicting 数据的路线轻微降权（0.9）。"""
    penalty = formulas.multipliers.conflict.penalty_factor
    return penalty if any(stop.place.verification_status == "conflicting" for stop in stops) else 1.0


# ── 总分 ────────────────────────────────────────────────────────────────────


def score_route(
    stops: Sequence[Stop],
    intent: Intent,
    *,
    scoring: ScoringConfig,
    archetype: ArchetypeName,
    relations: RelationIndex,
    budget: BudgetEstimate | None = None,
    walking_cap_m: int | None = None,
    weather: WeatherCondition | None = None,
    weights: Weights | None = None,
) -> ScoreBreakdown:
    """对一条已排好时间的路线打分。

    :param weights: 显式指定权重时使用（默认取 archetype 对应的权重组）。
    """
    formulas = scoring.formulas
    used_weights = weights or scoring.weights_for(archetype)
    metrics = route_metrics(stops)
    unknown_default = formulas.preference.unknown_score_default

    parts = {
        "preference": preference_score(stops, intent, scoring.preference_dimensions, formulas),
        "efficiency": route_efficiency(metrics, stops, formulas),
        "time_fit": time_fit(stops, formulas),
        "popularity": popularity_score(stops, formulas, unknown_default),
        "budget_fit": budget_fit(budget, _cap_per_person(intent), formulas),
        "walking_fit": walking_fit(metrics.walking_m, walking_cap_m),
        "place_relation": place_relation_score(stops, relations),
    }
    weighted = sum(getattr(used_weights, name) * value for name, value in parts.items())

    m_diversity = diversity_multiplier(stops, formulas)
    m_weather = weather_multiplier(stops, weather, formulas)
    m_conflict = conflict_multiplier(stops, formulas)
    total = clamp01(weighted * m_diversity * m_weather * m_conflict)

    return ScoreBreakdown(
        preference=parts["preference"],
        efficiency=parts["efficiency"],
        time_fit=parts["time_fit"],
        popularity=parts["popularity"],
        budget_fit=parts["budget_fit"],
        walking_fit=parts["walking_fit"],
        place_relation=parts["place_relation"],
        m_diversity=m_diversity,
        m_weather=m_weather,
        m_conflict=m_conflict,
        weighted_sum=weighted,
        total=total,
    )


def _cap_per_person(intent: Intent) -> Decimal | None:
    """用户预算上限（折算到人均）。不限预算返回 None。"""
    return budget_cap_per_person(intent.budget, intent.people)
