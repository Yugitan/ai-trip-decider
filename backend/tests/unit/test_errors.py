"""错误类型与错误码的单元测试。

核心要求（PRD §6 FR-13、§24）：
- 每个错误都要有**人话**提示与可执行建议
- 5xx 不得暴露堆栈
- HTTP 状态码映射必须稳定（前端依赖它做分支）
"""

from __future__ import annotations

import pytest

from app.core.errors import AppError, CostBreakerOpen, ErrorCode, ProviderError

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("code", list(ErrorCode))
def test_every_error_code_has_a_human_message_and_hint(code: ErrorCode) -> None:
    err = AppError(code)
    assert err.message, f"{code} 缺少面向用户的消息"
    assert err.hint, f"{code} 缺少可执行建议"
    # 不允许把枚举名直接丢给用户当消息
    assert err.message != str(code)


@pytest.mark.parametrize(
    ("code", "status"),
    [
        (ErrorCode.INVALID_INPUT, 422),
        (ErrorCode.TRIP_NOT_FOUND, 404),
        (ErrorCode.SHARE_NOT_FOUND, 404),
        (ErrorCode.FORBIDDEN, 403),
        (ErrorCode.RATE_LIMITED, 429),
        (ErrorCode.ADMIN_REQUIRED, 401),
        (ErrorCode.DB_UNAVAILABLE, 503),
        (ErrorCode.INTERNAL, 500),
    ],
)
def test_http_status_mapping(code: ErrorCode, status: int) -> None:
    assert AppError(code).http_status == status


def test_no_feasible_route_is_not_an_http_error() -> None:
    """排不出路线是"结果受限"，不是错误：必须返回 200 + 解释，而不是 5xx。"""
    assert AppError(ErrorCode.NO_FEASIBLE_ROUTE).http_status == 200
    assert AppError(ErrorCode.CANDIDATES_INSUFFICIENT).http_status == 200
    assert AppError(ErrorCode.COST_BREAKER_OPEN).http_status == 200


def test_custom_message_overrides_default_but_keeps_hint() -> None:
    err = AppError(ErrorCode.RATE_LIMITED, "今天 5 次免费规划已用完")
    assert err.message == "今天 5 次免费规划已用完"
    assert err.hint


def test_payload_shape_is_stable() -> None:
    payload = AppError(ErrorCode.PLACE_NOT_FOUND, context={"place_id": "x"}).to_payload()
    assert set(payload) == {"code", "message", "hint", "context"}
    assert payload["code"] == "PLACE_NOT_FOUND"
    assert payload["context"] == {"place_id": "x"}


def test_provider_error_carries_provider_and_operation() -> None:
    err = ProviderError("tavily", "search")
    assert err.provider == "tavily"
    assert err.operation == "search"
    assert err.code == ErrorCode.PROVIDER_UNAVAILABLE
    assert err.context == {"provider": "tavily", "operation": "search"}


def test_provider_error_kind_is_overridable() -> None:
    err = ProviderError("osrm", "route", kind=ErrorCode.PROVIDER_TIMEOUT)
    assert err.code == ErrorCode.PROVIDER_TIMEOUT
    assert err.http_status == 504


def test_cost_breaker_reports_amounts() -> None:
    err = CostBreakerOpen("llm", spent_cny=1.2, limit_cny=1.0)
    assert err.code == ErrorCode.COST_BREAKER_OPEN
    assert err.context["spent_cny"] == 1.2
    assert "1.2000" in err.message


def test_error_is_raisable_and_catchable_as_app_error() -> None:
    with pytest.raises(AppError) as exc:
        raise ProviderError("llm", "rerank")
    assert isinstance(exc.value, ProviderError)
    assert exc.value.code == ErrorCode.PROVIDER_UNAVAILABLE
