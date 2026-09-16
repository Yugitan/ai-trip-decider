"""行程费用估算（`app/domain/budget.py`，纯函数）。

★ 这个文件为什么存在 ★
全库 **0 条**地点有价格（TASKS.md B5），也就是说"餐饮站点没有价格"这个分支在真实
数据下**一定**会走到。曾经的实现在这一支上把整笔餐费从总额里静默地抹掉：一条
点都德 + 陶陶居的路线报出 `¥19.59/人`，而那 19.59 只是**一段打车费**
（起步价 12 + 2.6/km × 2.918km）。用户读到的是一个"预算"，实际是一张车票 ——
比"没有数字"更糟，因为它看起来已经算过了。

三条口径在这里被钉住：
1. 有餐饮站点但没价格 → 用配置里的餐费单价推定，并**指名道姓**写进
   `estimated_items`（"估算" ≠ "未知"）。
2. 有真实价格 → 用真实价格，不冒充估算。
3. 没被任何餐次计过的餐饮站点 → 价格未知时照样要说出来，不能当它不存在
   （甜品的钱不会因为 15:00 不属于任何餐次窗口就消失）。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.core.config import BudgetLimits, get_limits_config
from app.domain.budget import estimate_leg_cost, estimate_route_budget, meal_slots
from app.domain.models import Intent, Leg, Place, Stop

pytestmark = pytest.mark.unit

#: 点都德 → 光明广场：走 319m；光明广场 → 陶陶居：打车 2.918km。
#: 数字取自 2026-09-16 那条真实行程，好让断言对着**实际发生过**的读数。
WALK_319M = Leg(mode="walk", minutes=5, distance_m=319)
TAXI_2918M = Leg(mode="taxi", minutes=11, distance_m=2918)


def _place(name: str, category: str, price: Decimal | None = None) -> Place:
    return Place(
        id=name,
        name=name,
        category=category,
        lat=23.1,
        lng=113.2,
        price_min=price,
        price_max=price,
    )


def _stop(place: Place, arrive_min: int, stay_min: int, leg: Leg | None = None) -> Stop:
    return Stop(place=place, arrive_min=arrive_min, stay_min=stay_min, leg_to_next=leg)


def _limits() -> BudgetLimits:
    return get_limits_config().budget


def test_meal_windows_come_from_config_and_endpoints_do_not_overlap() -> None:
    """餐次窗口只从配置读，且端点相接不算（14:00 离开 ≠ 吃过午饭）。"""
    slots = {slot.name: slot for slot in meal_slots(_limits())}
    assert slots["breakfast"].start_min == 6 * 60 + 30
    assert slots["breakfast"].end_min == 10 * 60
    assert not slots["lunch"].overlaps(9 * 60, 11 * 60), "10:00-11:00 与午餐窗口不相交"
    assert not slots["lunch"].overlaps(14 * 60, 15 * 60), "刚好 14:00 开始不算吃了午饭（窗口右开）"
    assert slots["lunch"].overlaps(13 * 60, 14 * 60), "13:00-14:00 在午餐窗口内"


def test_food_stop_without_price_is_counted_as_estimate_not_dropped() -> None:
    """★ 本轮修的就是这条 ★：没有价格的茶楼，餐费也要进总额，并写明是估算。"""
    stops = (
        _stop(_place("点都德", "food"), 9 * 60, 90, WALK_319M),
        _stop(_place("光明广场", "shopping"), 10 * 60 + 35, 90, TAXI_2918M),
        _stop(_place("陶陶居", "food"), 12 * 60 + 16, 90),
    )
    estimate = estimate_route_budget(stops, Intent(), _limits())

    # 早餐（点都德，无价 → 配置 ¥30）+ 午餐（陶陶居，无价 → 配置 ¥60）+ 打车 19.59
    assert estimate.min_cny == Decimal("109.59"), f"预期 109.59，实得 {estimate.min_cny}"
    assert estimate.max_cny == estimate.min_cny
    assert estimate.estimated is True
    assert set(estimate.estimated_items) == {"点都德·breakfast", "陶陶居·lunch"}
    # 商场没有价格 → 仍然**不计入**并如实写出来（与上面那条正好相反）
    assert "光明广场·消费" in estimate.unknown_items


def test_real_prices_are_used_and_not_reported_as_estimates() -> None:
    """有价格就用价格：估算项必须是空的，否则"估算"这个词会变得没有信息量。"""
    stops = (
        _stop(_place("点都德", "food", Decimal("88")), 9 * 60, 90, WALK_319M),
        _stop(_place("陶陶居", "food", Decimal("120")), 12 * 60 + 16, 90),
    )
    estimate = estimate_route_budget(stops, Intent(), _limits())

    assert estimate.min_cny == Decimal("208.00") + Decimal("0")
    assert estimate.estimated_items == (), "单价来自地点本身时不该被写成估算项"
    assert estimate.unknown_items == ()


def test_meal_window_without_any_food_stop_still_gets_a_configured_estimate() -> None:
    """跨了饭点却没有餐饮站点：用配置单价补一笔，并写明"该时段无餐饮站点"。"""
    stops = (
        _stop(_place("广州塔", "attraction"), 9 * 60, 180, WALK_319M),
        _stop(_place("南信牛奶甜品专家", "food"), 15 * 60 + 30, 60),
    )
    estimate = estimate_route_budget(stops, Intent(), _limits())

    # 09:00-16:30 覆盖早餐与午餐，两段都没有餐饮站点 → ¥30 + ¥60
    assert estimate.estimated_items == ("该时段无餐饮站点·breakfast", "该时段无餐饮站点·lunch")
    # 甜品店在 15:30，不属于任何餐次窗口 —— 它没有被估算，但"没有价格"必须说出来
    assert "南信牛奶甜品专家·消费" in estimate.unknown_items
    assert estimate.min_cny == Decimal("90.00")


def test_walking_is_free_and_other_modes_are_charged_per_ride() -> None:
    """交通：步行一律 0 元（不按距离加价），打车按起步价 + 里程，地铁按次。"""
    limits = _limits()
    assert estimate_leg_cost(WALK_319M, limits) == Decimal("0")
    assert estimate_leg_cost(TAXI_2918M, limits) == Decimal("19.59")
    assert estimate_leg_cost(Leg(mode="metro", minutes=8, distance_m=1200), limits) == Decimal("4.0")
    assert estimate_leg_cost(Leg(mode="bus", minutes=6, distance_m=900), limits) == Decimal("2.0")


def test_no_stops_is_zero_and_not_estimated() -> None:
    """空行程：0 元、没有估算项。0 在这里是**真的** 0，不是"拿不到"的占位。"""
    estimate = estimate_route_budget((), Intent(), _limits())

    assert estimate.min_cny == Decimal("0")
    assert estimate.max_cny == Decimal("0")
    assert estimate.estimated is False
    assert estimate.estimated_items == ()
    assert estimate.unknown_items == ()
