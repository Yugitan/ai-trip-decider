"""领域数据结构（**纯数据 + 纯函数，零 IO**）。

为什么先写这个文件：
    M2 的评分 / 可行性 / 组合算法都必须能脱离数据库单测（架构铁律 1）。
    如果它们直接吃 SQLAlchemy 的 ``Place`` 模型，单测就不得不建库 —— 那 M2 的
    "秒级反馈"就没了。所以这里定义一组**扁平的不可变值对象**，由上层（M4 的
    plan_service）负责 ORM → 领域对象的映射。

设计取舍：
    - 全部 ``frozen=True``：评分与校验是纯函数，输入被篡改就谈不上可复现。
    - 时间统一用**从 0 点起的分钟数**（``arrive_min`` 等）。用 ``datetime`` 会让
      "一天内的行程"平白带上时区与日期问题，而可行性校验的全部判据都是
      "分钟级比较"（到达 + 停留 ≤ 下一段出发）。
    - 金额统一用 ``Decimal``：预算要参与"超预算 25% 剪枝"这类比较，浮点误差会让
      边界判定在测试里随机失败。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Literal

__all__ = [
    "DAY_MINUTES",
    "PACE_ORDER",
    "ArchetypeName",
    "BudgetEstimate",
    "BudgetSpec",
    "Constraint",
    "Intent",
    "Leg",
    "OpeningHours",
    "OpeningWindow",
    "Pace",
    "ParseResult",
    "Place",
    "Relation",
    "RelationIndex",
    "RouteMetrics",
    "RoutePlan",
    "Stop",
    "TransportMode",
    "TransportSource",
    "WeatherCondition",
    "hhmm_to_minutes",
    "minutes_to_hhmm",
    "route_metrics",
]

# ── 枚举型字面量 ────────────────────────────────────────────────────────────
# 与数据库 CHECK 约束保持一致（app/db/models.py）。放在这里是为了让领域函数的
# 签名自带文档，而不是到处传裸字符串。

Pace = Literal["relaxed", "balanced", "packed"]

#: 游玩时长偏好：半天 / 一天 / 用满用户给的时间窗。
#: 它是**每天**要排多久，不是整个行程多少天（那是 ``Intent.days``）。
DaySpan = Literal["half_day", "full_day", "whole_window"]
ArchetypeName = Literal["relaxed", "classic", "themed"]
TransportMode = Literal["walk", "bike", "metro", "bus", "taxi", "ferry"]
TransportSource = Literal["amap", "osrm", "estimated", "manual"]
WeatherCondition = Literal["clear", "rain", "heat"]

DAY_MINUTES = 24 * 60

# 节奏由松到紧的顺序。"带孩子 → 不允许比 balanced 更紧"这类约束需要比较节奏，
# 用字典查下标比写一串 if 更不容易漏。
PACE_ORDER: tuple[Pace, ...] = ("relaxed", "balanced", "packed")

TRANSPORT_MODES: tuple[TransportMode, ...] = ("walk", "bike", "metro", "bus", "taxi", "ferry")

# 允许作为"休息/用餐"语义的类别：两站之间的空等如果是为了吃饭休息，就不算空耗
REST_CATEGORIES: frozenset[str] = frozenset({"food", "cafe"})


# ── 时间工具 ────────────────────────────────────────────────────────────────


def hhmm_to_minutes(value: str) -> int:
    """``"09:30"`` → ``570``。只接受 ``HH:MM``，格式错误直接抛错（不猜）。

    >>> hhmm_to_minutes("09:30")
    570
    """
    hour_text, _, minute_text = value.partition(":")
    hour, minute = int(hour_text), int(minute_text or "0")
    if not 0 <= hour < 24 or not 0 <= minute < 60:
        raise ValueError(f"非法时间：{value!r}（应为 HH:MM）")
    return hour * 60 + minute


def minutes_to_hhmm(minutes: int) -> str:
    """``570`` → ``"09:30"``。跨天的时间按 24 小时取模（行程不会跨天安排）。"""
    normalized = minutes % DAY_MINUTES
    return f"{normalized // 60:02d}:{normalized % 60:02d}"


# ── 地点 ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class OpeningWindow:
    """一个开放时段（分钟，从 0 点起）。``last_entry_min`` 为最晚入场时刻。"""

    open_min: int
    close_min: int
    last_entry_min: int | None = None

    def __post_init__(self) -> None:
        if not 0 <= self.open_min < self.close_min <= DAY_MINUTES:
            raise ValueError(
                f"开放时段必须满足 0 <= open < close <= {DAY_MINUTES}，实际 {self}"
            )
        if self.last_entry_min is not None and not self.open_min <= self.last_entry_min <= self.close_min:
            raise ValueError(f"最晚入场时刻必须落在开放时段内：{self}")

    def covers(self, minutes: int) -> bool:
        """该时刻是否处于开放时段内（闭馆瞬间不算，即右开区间）。"""
        return self.open_min <= minutes < self.close_min


@dataclass(frozen=True, slots=True)
class OpeningHours:
    """按星期索引的开放时段。``0`` = 周一 … ``6`` = 周日（与 Python ``date.weekday()`` 一致）。

    空映射表示"完全没有营业时间数据"（对应数据库的 NULL），
    与"某天闭馆"（该天对应空元组）是**不同的两种状态**，不可混为一谈：
    前者必须报 HOURS_UNKNOWN，后者是确定的闭馆。
    """

    by_weekday: Mapping[int, tuple[OpeningWindow, ...]] = field(default_factory=dict)

    @property
    def is_unknown(self) -> bool:
        return not self.by_weekday

    def windows_at(self, weekday: int) -> tuple[OpeningWindow, ...] | None:
        """取某天的开放时段。``None`` = 未知，空元组 = 当天闭馆。"""
        if self.is_unknown:
            return None
        return self.by_weekday.get(weekday, ())


@dataclass(frozen=True, slots=True)
class Place:
    """规划用的地点快照（不是 ORM 实体，字段只保留算法真正要用的那些）。"""

    id: str
    name: str
    category: str
    lat: float
    lng: float

    district: str | None = None
    recommended_duration_min: int | None = None
    popularity_score: float | None = None
    # 偏好维度分值：键是 config/scoring.yaml 的 preference_dimensions 的键
    # （food / photo / culture / night_view / family / couple / citywalk / nature / shopping / museum）。
    # 缺失（None）与"确实是 0 分"必须区分，所以用 Mapping 而不是默认值填充。
    scores: Mapping[str, float | None] = field(default_factory=dict)

    price_min: Decimal | None = None
    price_max: Decimal | None = None
    opening_hours: OpeningHours | None = None
    indoor: bool | None = None
    rainy_day_score: float | None = None
    tags: tuple[str, ...] = ()
    verification_status: str = "unknown"
    status: str = "active"

    @property
    def is_active(self) -> bool:
        return self.status == "active"

    @property
    def has_hours(self) -> bool:
        """是否有可用的营业时间（None 或空都算没有）。"""
        return self.opening_hours is not None and not self.opening_hours.is_unknown


# ── 行程 ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Leg:
    """两站之间的一段通勤。

    ``source`` 是诚实性的关键：``estimated`` 表示耗时是"真实距离 × 文档化速度假设"
    推出来的，不是实测。UI 必须能看到它（PRD FR-05 AC-5.3）。
    """

    mode: TransportMode
    minutes: int
    distance_m: int
    source: TransportSource = "estimated"

    def __post_init__(self) -> None:
        if self.minutes < 0:
            raise ValueError(f"通勤耗时不能为负：{self}")
        if self.distance_m < 0:
            raise ValueError(f"通勤距离不能为负：{self}")

    @property
    def is_walk(self) -> bool:
        return self.mode == "walk"


@dataclass(frozen=True, slots=True)
class Stop:
    """行程中的一站。``leg_to_next`` 为 ``None`` 表示这是（当天）最后一站。

    ``day`` 从 1 开始。单日行程里它恒为 1（默认值），所以旧数据与旧调用不受影响；
    多日行程靠它把站点分回各自的"第几天"，而且 ``arrive_min`` **每天从 0 重新计**，
    不跨天累加 —— 第二天 09:00 就是 540，不是第 33 小时。
    """

    place: Place
    arrive_min: int
    stay_min: int
    leg_to_next: Leg | None = None
    day: int = 1

    @property
    def depart_min(self) -> int:
        return self.arrive_min + self.stay_min

    def on_day(self, day: int) -> Stop:
        """换到第 ``day`` 天（其余字段不变）。组合多日行程时用它给站点打标。"""
        return replace(self, day=day)


@dataclass(frozen=True, slots=True)
class RouteMetrics:
    """由站点序列推导出的可观测指标（可行性报告与评分都要用）。"""

    start_min: int
    end_min: int
    total_duration_min: int
    stay_min: int
    transit_min: int
    walking_m: int
    transit_distance_m: int
    place_count: int

    #: 这条行程横跨几天。1 = 单日（默认），与 ``intent.days`` 无关时也照实填。
    days: int = 1

    @property
    def travel_ratio(self) -> float:
        """通勤时间占总时长的比例。总时长为 0 时返回 0（而不是抛错）。"""
        if self.total_duration_min <= 0:
            return 0.0
        return self.transit_min / self.total_duration_min


def route_metrics(stops: Sequence[Stop]) -> RouteMetrics:
    """汇总一段行程的时长/步行/通勤指标。空行程返回全 0 而不是抛错。

    ``end_min`` 取最后一站的离开时刻 —— 行程的"结束"是人离开最后一个地点，
    不是到达。这个口径直接影响 WINDOW_OVERFLOW 的判定，写在这里避免两处不一致。
    """
    if not stops:
        return RouteMetrics(
            start_min=0,
            end_min=0,
            total_duration_min=0,
            stay_min=0,
            transit_min=0,
            walking_m=0,
            transit_distance_m=0,
            place_count=0,
        )

    stay = sum(stop.stay_min for stop in stops)
    transit = sum(stop.leg_to_next.minutes for stop in stops if stop.leg_to_next)
    walking = sum(stop.leg_to_next.distance_m for stop in stops if stop.leg_to_next and stop.leg_to_next.is_walk)
    distance = sum(stop.leg_to_next.distance_m for stop in stops if stop.leg_to_next)

    # 总时长 = **每天各自时长之和**，不是"第一天开始到最后一天结束"的跨度。
    # 跨天的差值里夹着一整夜的睡眠，把它当游玩时长会得出"两天游玩 33 小时"这种数；
    # 住宿也还没进路线。所以：2 天各排 8 小时 → 合计 16 小时。
    days = sorted({stop.day for stop in stops})
    per_day = 0
    for day in days:
        block = [stop for stop in stops if stop.day == day]
        per_day += max(0, block[-1].depart_min - block[0].arrive_min)

    return RouteMetrics(
        start_min=stops[0].arrive_min,
        end_min=stops[-1].depart_min,
        total_duration_min=per_day,
        stay_min=stay,
        transit_min=transit,
        walking_m=walking,
        transit_distance_m=distance,
        place_count=len(stops),
        days=len(days),
    )


@dataclass(frozen=True, slots=True)
class BudgetEstimate:
    """一段行程的费用估算。

    ★ 诚实性约定 ★
    三类项目分得清清楚楚，因为它们的实际含义完全不同：

    - ``unknown_items``：**没有计入金额**的项目（没有可靠价格）。
      它们**不按 0 元计** —— 那会让预算看起来比实际低，是变相的编造。
    - ``estimated_items``：**计入了金额、但单价是假设**的项目（餐费没有价格时
      用 config/limits.yaml 里的餐费单价推定，交通费同理）。
      "估算"不等于"未知"：一个是"我猜了、并告诉你猜的什么"，一个是"我没算"。
    - 剩下的就是有来源的真实价格。

    ``estimated`` 为 True 表示金额中含有估算值（交通费/餐费单价来自
    config/limits.yaml 的可审计假设，不是真实报价）。
    """

    min_cny: Decimal
    max_cny: Decimal
    unknown_items: tuple[str, ...] = ()
    estimated_items: tuple[str, ...] = ()
    estimated: bool = True
    scope: Literal["per_person", "total"] = "per_person"


@dataclass(frozen=True, slots=True)
class RoutePlan:
    """一条完整方案（站点已排好时间）。"""

    archetype: ArchetypeName
    stops: tuple[Stop, ...]
    theme: str | None = None
    budget: BudgetEstimate | None = None

    @property
    def place_ids(self) -> tuple[str, ...]:
        return tuple(stop.place.id for stop in self.stops)

    @property
    def metrics(self) -> RouteMetrics:
        return route_metrics(self.stops)


# ── 地点关系 ────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Relation:
    """两个地点之间的距离/耗时/组合质量。

    ``minutes_by_mode`` 只填**有数据**的出行方式：缺失表示"不知道"，
    调用方必须回退到估算并标记 estimated，绝不能当成 0 分钟。
    """

    a_id: str
    b_id: str
    distance_m: int | None = None
    minutes_by_mode: Mapping[TransportMode, int] = field(default_factory=dict)
    relationship_score: float | None = None
    derived_modes: tuple[str, ...] = ()
    source: TransportSource = "estimated"

    def minutes_for(self, mode: TransportMode) -> int | None:
        return self.minutes_by_mode.get(mode)


@dataclass(frozen=True, slots=True)
class RelationIndex:
    """按地点对索引的关系图。键统一为"(小 id, 大 id)"，与数据库一致。"""

    by_pair: Mapping[tuple[str, str], Relation] = field(default_factory=dict)

    @classmethod
    def build(cls, relations: Sequence[Relation]) -> RelationIndex:
        return cls(by_pair={(_pair_key(r.a_id, r.b_id)): r for r in relations})

    def lookup(self, a_id: str, b_id: str) -> Relation | None:
        if a_id == b_id:
            return None
        return self.by_pair.get(_pair_key(a_id, b_id))

    def score_between(self, a_id: str, b_id: str) -> float | None:
        relation = self.lookup(a_id, b_id)
        return relation.relationship_score if relation else None

    def __len__(self) -> int:
        return len(self.by_pair)


def _pair_key(a_id: str, b_id: str) -> tuple[str, str]:
    return (a_id, b_id) if a_id <= b_id else (b_id, a_id)


# ── 意图 ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class BudgetSpec:
    amount: Decimal | None = None
    scope: Literal["per_person", "total"] = "per_person"
    currency: str = "CNY"

    @property
    def is_unlimited(self) -> bool:
        """0 或 None 表示不限制预算（PRD FR-01：0 = 不限制）。"""
        return self.amount is None or self.amount <= 0


@dataclass(frozen=True, slots=True)
class Constraint:
    """一条硬约束。``raw`` 保留原文，便于"为什么排除了它"可追溯。"""

    type: str
    value: object
    raw: str = ""
    source: str = "free_text"


@dataclass(frozen=True, slots=True)
class Intent:
    """结构化的用户需求（PRD FR-02 的 ``intent`` 对象）。"""

    city: str = "guangzhou"
    days: int = 1
    day_span: DaySpan = "full_day"
    people: int = 2
    preferences: Mapping[str, float] = field(default_factory=dict)
    pace: Pace = "relaxed"
    budget: BudgetSpec = BudgetSpec()
    start_min: int = hhmm_to_minutes("09:00")
    end_min: int = hhmm_to_minutes("21:00")
    travel_date: str | None = None  # ISO date 字符串；None 表示"未指定"
    weather_sensitive: bool = True

    @property
    def window_min(self) -> int:
        return max(0, self.end_min - self.start_min)

    @property
    def active_preferences(self) -> Mapping[str, float]:
        """只保留权重 > 0 的偏好维度（coverage 的分母用它）。"""
        return {dim: weight for dim, weight in self.preferences.items() if weight > 0}


@dataclass(frozen=True, slots=True)
class ParseResult:
    """意图解析结果。

    ``unparsed`` 是交给 LLM 的原文片段 —— 规则引擎**不许**假装自己解析了全部内容
    （PRD FR-02 AC-2.3：规则引擎独立可用，但覆盖不到的要如实交出去）。
    """

    intent: Intent
    constraints: tuple[Constraint, ...] = ()
    applied_rules: tuple[str, ...] = ()
    unparsed: tuple[str, ...] = ()
    parse_source: Literal["rule", "llm", "cache", "hybrid"] = "rule"
