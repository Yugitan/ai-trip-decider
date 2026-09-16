"""多日行程的领域语义：每天多长、指标怎么合计、各天的报告怎么合并。

为什么单独立一个文件：这几条都是**口径**问题，而口径错了不会报错 ——
只会给出一个看起来合理的数字。例如把"两天各 8 小时"算成"33 小时"
（把中间那夜的睡眠也算成游玩时长），或者第二天的 4 顿餐费里只算 1 顿。
这些只能靠测试钉住，不能靠代码评审看出。

（对外行为在 ``tests/integration/test_multi_day.py`` 里守。）
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.core.config import get_limits_config
from app.domain.budget import estimate_route_budget
from app.domain.feasibility import FeasibilityReport, Violation
from app.domain.models import (
    BudgetSpec,
    Intent,
    Leg,
    Place,
    RouteMetrics,
    Stop,
    route_metrics,
)
from app.domain.planner import day_budget_minutes, day_window
from app.services.plan_service import _merge_reports, _merge_score_breakdown

pytestmark = pytest.mark.unit

LIMITS = get_limits_config()


def _stop(
    name: str, arrive: int, stay: int, day: int = 1, leg: Leg | None = None
) -> Stop:
    return Stop(
        place=Place(id=f"{name}-{day}", name=name, category="attraction", lat=23.1, lng=113.2),
        arrive_min=arrive,
        stay_min=stay,
        leg_to_next=leg,
        day=day,
    )


# ── 每排多久 ────────────────────────────────────────────────────────────────


def _intent(**overrides: object) -> Intent:
    return Intent(**{"start_min": 9 * 60, "end_min": 21 * 60, **overrides})  # type: ignore[arg-type]


def test_half_day_is_capped_by_the_configured_span() -> None:
    """时间窗有 12 小时，选了「半天」也只能排半天。"""
    budget = day_budget_minutes(_intent(day_span="half_day"), LIMITS)
    assert budget == LIMITS.planning.day_span_minutes["half_day"]
    assert budget < 12 * 60


def test_the_user_window_still_wins_when_it_is_shorter() -> None:
    """窗口比时长档更短时以窗口为准 —— 用户写的时间是硬约束。"""
    narrow = _intent(day_span="full_day", end_min=12 * 60)
    assert day_budget_minutes(narrow, LIMITS) == 3 * 60


def test_whole_window_uses_the_entire_window() -> None:
    assert day_budget_minutes(_intent(day_span="whole_window"), LIMITS) == 12 * 60


def test_day_window_starts_every_day_at_the_same_time() -> None:
    """每天同一套时刻：第二天 09:00 就是 540，不是第 33 小时。"""
    start, end = day_window(_intent(day_span="full_day"), LIMITS)
    assert (start, end) == (9 * 60, 9 * 60 + LIMITS.planning.day_span_minutes["full_day"])


# ── 指标合计 ────────────────────────────────────────────────────────────────


def test_multi_day_total_is_the_sum_of_days_not_the_overnight_span() -> None:
    """★ 两天的总时长 = 各天之和，不是\"从第一天早上到最后一天晚上\" ★

    后者会把中间那夜的睡眠算进来（2 天 → 33 小时），而住宿根本没进路线。
    """
    stops = (
        _stop("A", 9 * 60, 120, day=1),
        _stop("B", 15 * 60, 60, day=1),
        _stop("C", 9 * 60, 120, day=2),
        _stop("D", 15 * 60, 60, day=2),
    )
    metrics = route_metrics(stops)
    # 每天 9:00→16:00 = 420 分钟，两天 840；跨天跨度则是 33 小时
    assert metrics.total_duration_min == 840
    assert metrics.days == 2
    assert metrics.place_count == 4


def test_single_day_metrics_are_unchanged() -> None:
    """单日行程的指标口径不能因为引入多日而变化。"""
    metrics = route_metrics((_stop("A", 9 * 60, 90), _stop("B", 11 * 60, 90)))
    assert metrics.total_duration_min == 11 * 60 + 90 - 9 * 60
    assert metrics.days == 1


def test_empty_route_still_reports_one_day() -> None:
    metrics = route_metrics(())
    assert metrics.total_duration_min == 0
    assert metrics.days == 1


# ── 预算：餐次是每天各一次 ──────────────────────────────────────────────────


def test_each_day_pays_for_its_own_meals() -> None:
    """★ 两天的行程吃两顿午饭 ★：整段当成连续时间会让第二天整天的饭钱消失。"""
    intent = _intent(budget=BudgetSpec(amount=Decimal("999")))
    one_day = estimate_route_budget((_stop("A", 12 * 60, 60),), intent, LIMITS.budget)
    two_days = estimate_route_budget(
        (_stop("A", 12 * 60, 60, day=1), _stop("B", 12 * 60, 60, day=2)), intent, LIMITS.budget
    )
    assert two_days.min_cny == one_day.min_cny * 2, (
        f"单日 {one_day.min_cny} → 两日 {two_days.min_cny}，餐次没有按天重复"
    )
    # 两顿都要说清是"估算"，而不是只报第一顿
    assert len(two_days.estimated_items) == len(one_day.estimated_items) * 2


# ── 合并各天的报告与评分 ────────────────────────────────────────────────────


def _report(*, at_seq: int | None, place_count: int, message: str = "w") -> FeasibilityReport:
    return FeasibilityReport(
        feasible=True,
        violations=(),
        warnings=(Violation(code="HOURS_UNKNOWN", severity="soft", message=message, at_seq=at_seq),),
        metrics=RouteMetrics(
            start_min=0,
            end_min=0,
            total_duration_min=0,
            stay_min=0,
            transit_min=0,
            walking_m=0,
            transit_distance_m=0,
            place_count=place_count,
        ),
    )


def test_merged_report_shifts_warning_positions_by_the_previous_days() -> None:
    """★ 第二天的告警必须挂到合并后的正确站点上 ★

    ``at_seq`` 是站点在列表里的下标。不平移的话，第二天的告警会全部挂到第一天的
    同名下标上 —— 位置错了比没位置更难发现（用户会以为问题出在另一站）。
    """
    stops = (
        _stop("A", 9 * 60, 60, day=1),
        _stop("B", 10 * 60, 60, day=1),
        _stop("C", 9 * 60, 60, day=2),
    )
    merged = _merge_reports(
        [_report(at_seq=0, place_count=2, message="第一天"), _report(at_seq=0, place_count=1, message="第二天")],
        stops,
    )
    positions = {item.message: item.at_seq for item in merged.warnings}
    assert positions == {"第一天": 0, "第二天": 2}


def test_merged_report_keeps_route_level_warnings_at_none() -> None:
    """``at_seq=None`` 表示\"整条路线\"，它不能因为平移变成一个具体下标。"""
    stops = (_stop("A", 9 * 60, 60, day=1), _stop("B", 9 * 60, 60, day=2))
    merged = _merge_reports(
        [_report(at_seq=None, place_count=1), _report(at_seq=None, place_count=1)], stops
    )
    assert [item.at_seq for item in merged.warnings] == [None, None]


def test_merged_metrics_cover_all_days() -> None:
    stops = (_stop("A", 9 * 60, 60, day=1), _stop("B", 9 * 60, 60, day=2))
    merged = _merge_reports([_report(at_seq=None, place_count=1)] * 2, stops)
    assert merged.metrics.place_count == 2
    assert merged.metrics.days == 2


def _breakdown(total: float) -> object:
    from app.domain.scoring import ScoreBreakdown

    fields = (
        "preference",
        "efficiency",
        "time_fit",
        "popularity",
        "budget_fit",
        "walking_fit",
        "place_relation",
        "m_diversity",
        "m_weather",
        "m_conflict",
        "weighted_sum",
    )
    return ScoreBreakdown(**dict.fromkeys(fields, total), total=total)


def test_merged_score_is_the_average_of_the_days() -> None:
    """第二天排得糟不能因为第一天漂亮就被盖住，也不能因为\"哪天长\"而加权。"""
    merged = _merge_score_breakdown([_breakdown(0.9), _breakdown(0.5)])  # type: ignore[list-item]
    assert merged.total == pytest.approx(0.7)
    assert merged.preference == pytest.approx(0.7)


def test_merging_nothing_is_an_error_not_a_zero_score() -> None:
    """空列表返回 0 分会让\"没有一天排出来\"看起来像\"排了但很差\"。"""
    with pytest.raises(ValueError):
        _merge_score_breakdown([])


def test_stop_day_defaults_to_one_and_is_copyable() -> None:
    """``day`` 的默认值保证旧调用方与新数据都能用同一个构造器。"""
    stop = _stop("A", 9 * 60, 60)
    assert stop.day == 1
    assert stop.on_day(3).day == 3
    assert stop.on_day(3).arrive_min == stop.arrive_min
