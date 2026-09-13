"""行程费用估算（**纯函数，零 IO**）。

★ 这个模块最重要的不是"算出多少钱"，而是**诚实地区分三件事** ★

1. **已知价格**：地点的 ``price_min/price_max``（有来源的门票/人均）。
2. **估算价格**：餐费与交通费，单价来自 ``config/limits.yaml`` 的可审计假设
   （早茶 ¥30 / 午餐 ¥60 / 晚餐 ¥80，地铁 ¥4…），结果一律标记 ``estimated``。
3. **未知价格**：地点没有 ``price_*`` 字段。这种情况**不计入金额**，而是记进
   ``unknown_items`` —— 按 0 元计会让预算看起来比实际低，等于变相编造。

MVP 没有可靠的餐饮/门票价格来源（见 TASKS.md B5），所以预算整体是"量级参考"，
不是报价。宁可留空，不可编造。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from app.core.config import BudgetLimits
from app.domain.models import (
    REST_CATEGORIES,
    BudgetEstimate,
    BudgetSpec,
    Intent,
    Leg,
    Stop,
    hhmm_to_minutes,
)

__all__ = [
    "MealSlot",
    "budget_cap_per_person",
    "budget_gap",
    "estimate_leg_cost",
    "estimate_route_budget",
    "meal_slots",
    "to_per_person",
    "to_total",
]

_CENTS = Decimal("0.01")


def _money(value: Decimal) -> Decimal:
    return value.quantize(_CENTS, rounding=ROUND_HALF_UP)


@dataclass(frozen=True, slots=True)
class MealSlot:
    """一个餐次：名称与它覆盖的时间区间（分钟，从 0 点起）。"""

    name: str
    start_min: int
    end_min: int

    def overlaps(self, start_min: int, end_min: int) -> bool:
        """行程（或某个停留）是否与该餐次有交集。端点相接不算（刚好 14:00 离开
        不算吃过午饭，因为 14:00 是午餐窗口的结束）。"""
        return start_min < self.end_min and end_min > self.start_min


def meal_slots(limits: BudgetLimits) -> tuple[MealSlot, ...]:
    """从配置读出餐次时间窗。配置里没定义时间窗的餐次不参与估算。"""
    slots = [
        MealSlot(name, hhmm_to_minutes(window["start"]), hhmm_to_minutes(window["end"]))
        for name, window in limits.meal_windows.items()
    ]
    return tuple(sorted(slots, key=lambda slot: slot.start_min))


def to_total(amount: Decimal, scope: str, people: int) -> Decimal:
    """人均 → 总计。"""
    if scope == "total" or people <= 1:
        return _money(amount)
    return _money(amount * people)


def to_per_person(amount: Decimal, scope: str, people: int) -> Decimal:
    """总计 → 人均。``people`` 为 0 时按 1 人处理，避免除零（人数由上层校验 ≥1）。"""
    if scope == "per_person" or people <= 1:
        return _money(amount)
    return _money(amount / people)


def budget_cap_per_person(budget: BudgetSpec, people: int) -> Decimal | None:
    """用户的预算上限（统一折算到人均）。不限预算时返回 ``None``。"""
    if budget.is_unlimited:
        return None
    assert budget.amount is not None  # is_unlimited 已排除 None
    return to_per_person(budget.amount, budget.scope, max(1, people))


def estimate_leg_cost(leg: Leg, limits: BudgetLimits) -> Decimal:
    """一段通勤的费用（人均）。

    步行/骑行免费；出租车 = 起步价 + 单价 × 公里；其余按次计费。
    这些都是 config/limits.yaml 里的保守假设，不是真实报价。
    """
    fare = limits.transit_fare_cny
    if leg.mode in ("walk", "bike"):
        return Decimal("0")
    if leg.mode == "taxi":
        per_km = Decimal(str(fare.get("taxi_per_km", 0)))
        base = Decimal(str(fare.get("taxi_base", 0)))
        return _money(base + per_km * (Decimal(leg.distance_m) / 1000))
    return _money(Decimal(str(fare.get(f"{leg.mode}_per_ride", 0))))


def estimate_route_budget(
    stops: Sequence[Stop],
    intent: Intent,
    limits: BudgetLimits,
) -> BudgetEstimate:
    """估算一段行程的人均/总费用。

    计量口径：
    - 门票：非餐饮类地点按 ``price_min/price_max``；缺失 → 计入 unknown，不按 0 算。
    - 餐饮：行程覆盖到的餐次各算一次。若该餐次时段内**有**餐饮类站点，
      则用该站点的价格（缺失 → unknown）；否则用配置里的餐费估算值。
    - 交通：按段计费（见 :func:`estimate_leg_cost`）。
    """
    if not stops:
        return BudgetEstimate(
            min_cny=Decimal("0"),
            max_cny=Decimal("0"),
            unknown_items=(),
            estimated=False,
            scope=intent.budget.scope,
        )

    min_cny = Decimal("0")
    max_cny = Decimal("0")
    unknown: list[str] = []
    estimated = False

    # ── 门票 ──
    for stop in stops:
        if stop.place.category in REST_CATEGORIES:
            continue  # 餐饮类在下面的餐次逻辑里处理，避免重复计费
        price_min, price_max = stop.place.price_min, stop.place.price_max
        if price_min is None and price_max is None:
            unknown.append(f"{stop.place.name}·消费")
            continue
        min_cny += price_min if price_min is not None else (price_max or Decimal("0"))
        max_cny += price_max if price_max is not None else (price_min or Decimal("0"))

    # ── 餐饮 ──
    route_start, route_end = stops[0].arrive_min, stops[-1].depart_min
    for slot in meal_slots(limits):
        if not slot.overlaps(route_start, route_end):
            continue
        meal_stop = next(
            (
                stop
                for stop in stops
                if stop.place.category in REST_CATEGORIES
                and slot.overlaps(stop.arrive_min, stop.depart_min)
            ),
            None,
        )
        if meal_stop is None:
            # 行程跨过了饭点却没有餐饮站点 —— 按配置估价补一笔，并标记 estimated。
            cost = Decimal(str(limits.meal_cost_cny.get(slot.name, 0)))
            min_cny += cost
            max_cny += cost
            estimated = True
            continue
        price_min, price_max = meal_stop.place.price_min, meal_stop.place.price_max
        if price_min is None and price_max is None:
            unknown.append(f"{meal_stop.place.name}·{slot.name}")
            continue
        min_cny += price_min if price_min is not None else (price_max or Decimal("0"))
        max_cny += price_max if price_max is not None else (price_min or Decimal("0"))

    # ── 交通 ──
    for stop in stops:
        if stop.leg_to_next is None:
            continue
        cost = estimate_leg_cost(stop.leg_to_next, limits)
        min_cny += cost
        max_cny += cost
        if not stop.leg_to_next.is_walk:
            estimated = True

    people = max(1, intent.people)
    scope = intent.budget.scope
    return BudgetEstimate(
        min_cny=to_total(_money(min_cny), "per_person", people) if scope == "total" else _money(min_cny),
        max_cny=to_total(_money(max_cny), "per_person", people) if scope == "total" else _money(max_cny),
        unknown_items=tuple(unknown),
        estimated=estimated,
        scope=scope,
    )


def budget_gap(estimate: BudgetEstimate, budget: BudgetSpec, people: int) -> Mapping[str, Decimal]:
    """与用户预算的差额（正 = 超支）。不限预算时返回 0 差额。"""
    cap = budget_cap_per_person(budget, people)
    zero = Decimal("0")
    if cap is None:
        return {"over_cny": zero, "under_cny": zero}
    per_person = to_per_person(estimate.max_cny, estimate.scope, people)
    diff = per_person - cap
    return {"over_cny": _money(diff) if diff > 0 else zero, "under_cny": _money(-diff) if diff < 0 else zero}
