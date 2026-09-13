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
from app.domain.models import Constraint, Intent, ParseResult
from app.schemas.trips import PlanRequest
from app.services.plan_service import build_intent, intent_to_dict

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
