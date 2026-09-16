"""规划编排的入参构造单元测试（PRD FR-01 / FR-02）。

这一层不碰数据库，守的是"用户输入如何变成意图"这条最容易被静默放过的路径：
- 表单默认值必须来自 ``config/limits.yaml``（而不是散落在代码里的 09:00/21:00）；
- 未知偏好、非法时间、过长的自由文本都必须**明确报错**，不能静默忽略；
- 自由文本里的规则可以覆盖表单值（用户后说的为准）。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.core.config import get_limits_config, get_scoring_config
from app.core.errors import AppError, ErrorCode
from app.domain.feasibility import FeasibilityReport, Violation
from app.domain.models import Constraint, Intent, ParseResult, RoutePlan, route_metrics
from app.domain.models import Place as DomainPlace
from app.domain.scoring import ScoreBreakdown
from app.schemas.trips import PlanRequest
from app.services.plan_service import (
    _cons,
    _ScoredPlan,
    _stop_why,
    build_intent,
    intent_to_dict,
)

pytestmark = pytest.mark.unit


def _build(**overrides: object) -> tuple[Intent, tuple[Constraint, ...], ParseResult]:
    payload = PlanRequest(**{"city": "guangzhou", **overrides})
    return build_intent(payload, limits=get_limits_config(), scoring=get_scoring_config())


def test_defaults_come_from_config_not_hardcoded() -> None:
    limits = get_limits_config()
    intent, constraints, parsed = _build()
    assert intent.city == "guangzhou"
    assert intent.people == 2
    assert intent.pace == "relaxed"
    assert intent.days == 1
    # 默认时间窗必须与配置一致：两处硬编码迟早会漂移
    from app.domain.models import hhmm_to_minutes

    assert intent.start_min == hhmm_to_minutes(limits.planning.default_window["start"])
    assert intent.end_min == hhmm_to_minutes(limits.planning.default_window["end"])
    assert constraints == ()
    assert parsed.parse_source == "rule"


def test_budget_scope_is_preserved() -> None:
    intent, _, _ = _build(budget={"amount": Decimal("300"), "scope": "total"})
    assert intent.budget.amount == Decimal("300")
    assert intent.budget.scope == "total"


def test_no_budget_means_unlimited() -> None:
    intent, _, _ = _build(budget=None)
    assert intent.budget.is_unlimited


def test_preferences_become_weights() -> None:
    intent, _, _ = _build(preferences=["food", "photo"])
    assert set(intent.preferences) == {"food", "photo"}
    assert all(weight == 1.0 for weight in intent.preferences.values())


def test_unknown_preference_is_rejected() -> None:
    """未知偏好必须报错：静默忽略会让用户以为"我选了夜景"。"""
    with pytest.raises(AppError) as exc:
        _build(preferences=["food", "潜水"])
    assert exc.value.code == ErrorCode.INVALID_INPUT
    assert "潜水" in exc.value.message


def test_free_text_overrides_form_values() -> None:
    intent, constraints, _ = _build(pace="relaxed", free_text="想轻松一点，预算 500 元")
    assert intent.budget.amount == Decimal("500")
    assert constraints == ()


def test_free_text_exclusion_becomes_constraint() -> None:
    """排除项必须是**真实地点名**："不要去广州塔" 不能变成 "去广州塔"，
    否则重算时它匹配不上任何地点，用户的修改被静默忽略。
    """
    _, constraints, _ = _build(free_text="不要去广州塔")
    assert [c.type for c in constraints] == ["exclude_place"]
    assert constraints[0].value == "广州塔"


def test_free_text_too_long_is_rejected() -> None:
    limits = get_limits_config()
    too_long = "广" * (limits.rate_limit.input_free_text_max_chars + 1)
    with pytest.raises(AppError) as exc:
        _build(free_text=too_long)
    assert exc.value.code == ErrorCode.INVALID_INPUT
    assert exc.value.context["max_chars"] == limits.rate_limit.input_free_text_max_chars


def test_invalid_time_format_is_rejected() -> None:
    with pytest.raises(AppError) as exc:
        _build(start_time="早上九点")
    assert exc.value.code == ErrorCode.INVALID_INPUT


def test_start_after_end_is_rejected() -> None:
    with pytest.raises(AppError) as exc:
        _build(start_time="18:00", end_time="09:00")
    assert exc.value.code == ErrorCode.INVALID_INPUT


def test_valid_time_window_is_used() -> None:
    intent, _, _ = _build(start_time="10:30", end_time="19:00")
    assert intent.start_min == 10 * 60 + 30
    assert intent.end_min == 19 * 60


def test_intent_to_dict_is_json_serialisable_shape() -> None:
    intent, _, _ = _build(budget={"amount": Decimal("300"), "scope": "per_person"})
    payload = intent_to_dict(intent)
    assert payload["start_time"] == "09:00"
    assert payload["end_time"] == "21:00"
    assert payload["budget"]["amount"] == "300", "金额用字符串，避免 jsonb 里变成浮点"
    assert payload["budget"]["scope"] == "per_person"
    assert payload["preferences"] == {}


def test_intent_to_dict_handles_unlimited_budget() -> None:
    intent, _, _ = _build(budget=None)
    assert intent_to_dict(intent)["budget"]["amount"] is None


# ── 「为什么推荐这一站」────────────────────────────────────────────────────
#
# 守的是"文案不空泛"的可核验版本：一个只写「美食」的徽章是**用户自己勾的偏好**
# 的复述 —— 同一趟里两座茶楼会长得一模一样。所以文案里必须出现可回溯的数据。


def _domain_place(*, scores: dict[str, float | None], tags: tuple[str, ...]) -> DomainPlace:
    return DomainPlace(
        id="p1",
        name="测试茶楼",
        category="food",
        lat=23.1,
        lng=113.2,
        scores=scores,
        tags=tags,
    )


def _why(scores: dict[str, float | None], tags: tuple[str, ...], **overrides: object) -> str | None:
    intent, _, _ = _build(**{"preferences": ["food"], **overrides})
    return _stop_why(_domain_place(scores=scores, tags=tags), intent, get_scoring_config())


def test_stop_why_carries_the_score_not_just_the_label() -> None:
    reason = _why({"food": 0.9}, ("美食", "早茶"))
    assert reason is not None
    assert "美食" in reason and "0.90" in reason, "分值必须写出来，否则理由无法核验"
    # 与维度标签重复的 tag 不得再出现一次（"美食 0.90 · 美食" 是纯噪音）
    assert reason.count("美食") == 1
    assert "早茶" in reason, "地点自己的标签才是区分两座茶楼的东西"


def test_stop_why_distinguishes_two_places_with_the_same_label() -> None:
    strong = _why({"food": 0.9}, ("美食", "早茶", "老字号"))
    weak = _why({"food": 0.65}, ("美食",))
    assert strong != weak


def test_stop_why_is_none_below_threshold() -> None:
    threshold = get_scoring_config().formulas.preference.coverage_min_dim_score
    assert _why({"food": threshold - 0.01}, ("美食",)) is None


def test_stop_why_is_none_without_preferences() -> None:
    intent, _, _ = _build()
    assert _stop_why(_domain_place(scores={"food": 0.9}, tags=()), intent, get_scoring_config()) is None


def test_stop_why_has_no_trailing_separator() -> None:
    """没有可用 tag 时不能留下尾随的 " · "（在浅色徽章里是一道明显的黑点）。"""
    reason = _why({"food": 0.9}, ("美食",))
    assert reason is not None and not reason.endswith("·")


def test_stop_why_orders_by_score_and_is_deterministic() -> None:
    """多个维度都命中时，最强的排前面；同分时顺序固定（同一输入同一文案）。"""
    first = _why({"food": 0.7, "photo": 0.95}, (), preferences=["food", "photo"])
    second = _why({"food": 0.7, "photo": 0.95}, (), preferences=["photo", "food"])
    assert first == second
    assert first is not None and first.index("拍照") < first.index("美食")


def test_stop_why_caps_the_badge_at_two_dimensions() -> None:
    """徽章是一行的：命中五个维度也不能把它写成一段话。"""
    reason = _why(
        {"food": 0.9, "photo": 0.9, "culture": 0.9, "shopping": 0.9},
        (),
        preferences=["food", "photo", "culture", "shopping"],
    )
    assert reason is not None
    assert reason.count("0.90") == 2


# ── 「需要留意」（`_cons`）──────────────────────────────────────────────────
#
# `_cons` 只读 `report.warnings`，所以这里的假方案除了告警之外全是空壳。


def _cons_plan(*messages: str) -> _ScoredPlan:
    report = FeasibilityReport(
        feasible=True,
        violations=(),
        warnings=tuple(
            Violation(
                code="HOURS_NOT_CHECKED",
                severity="soft",
                at_seq=index,
                message=message,
            )
            for index, message in enumerate(messages, start=1)
        ),
        metrics=route_metrics(()),
    )
    return _ScoredPlan(
        plan=RoutePlan(archetype="classic", stops=()),
        archetype="classic",
        report=report,
        breakdown=ScoreBreakdown(
            preference=0.0,
            efficiency=0.0,
            time_fit=0.0,
            popularity=0.0,
            budget_fit=0.0,
            walking_fit=0.0,
            place_relation=0.0,
            m_diversity=0.0,
            m_weather=0.0,
            m_conflict=0.0,
            weighted_sum=0.0,
            total=0.0,
        ),
        source="generated",
    )


def test_cons_never_repeats_the_same_sentence() -> None:
    """三个站点都没填出行日期，不能变成三条一模一样的「需要留意」。

    营业时间提示是**按站点**逐条产生的，文案里不带站名（哪一站的信息在
    `detail.place` 里），所以站点一多就必然重复。前端的列表以句子为 key，
    重复句子会直接让 React 报 duplicate key。
    """
    line = "未指定出行日期，无法校验营业时间（景点常按周几闭馆）"
    item = _cons_plan(line, line, line)

    assert _cons(item) == [line]


def test_cons_caps_at_three_distinct_lines() -> None:
    """去重后再取 3 条：四条不同提示时给 3 条**不同**的，而不是重复 2 条。"""
    item = _cons_plan("甲", "甲", "乙", "丙", "丁")

    assert _cons(item) == ["甲", "乙", "丙"]
