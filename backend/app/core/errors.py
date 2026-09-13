"""统一错误类型与错误码。

原则（PRD §6 FR-13）：
- 任何 5xx 都不得向用户暴露堆栈。
- 每个错误都要有**人话提示**与**可执行建议**，而不是技术术语。
- 错误必须带 request_id，便于用户反馈时定位。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    # 输入
    INVALID_INPUT = "INVALID_INPUT"
    UNSUPPORTED_CITY = "UNSUPPORTED_CITY"

    # 数据
    NO_PLACES_FOR_CITY = "NO_PLACES_FOR_CITY"
    CANDIDATES_INSUFFICIENT = "CANDIDATES_INSUFFICIENT"
    NO_FEASIBLE_ROUTE = "NO_FEASIBLE_ROUTE"
    NOT_FOUND = "NOT_FOUND"
    TRIP_NOT_FOUND = "TRIP_NOT_FOUND"
    PLACE_NOT_FOUND = "PLACE_NOT_FOUND"
    SHARE_NOT_FOUND = "SHARE_NOT_FOUND"

    # 权限与限流
    FORBIDDEN = "FORBIDDEN"
    RATE_LIMITED = "RATE_LIMITED"
    ADMIN_REQUIRED = "ADMIN_REQUIRED"

    # 外部依赖（可降级）
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    PROVIDER_TIMEOUT = "PROVIDER_TIMEOUT"
    PROVIDER_INVALID_RESPONSE = "PROVIDER_INVALID_RESPONSE"

    # 系统
    COST_BREAKER_OPEN = "COST_BREAKER_OPEN"
    DB_UNAVAILABLE = "DB_UNAVAILABLE"
    INTERNAL = "INTERNAL"


_DEFAULT_HINTS: dict[ErrorCode, str] = {
    ErrorCode.INVALID_INPUT: "请检查输入内容后重试。",
    ErrorCode.UNSUPPORTED_CITY: "目前只支持广州，其他城市正在陆续开放。",
    ErrorCode.NO_PLACES_FOR_CITY: "该城市的知识库还没有数据，请稍后再试或换一个城市。",
    ErrorCode.CANDIDATES_INSUFFICIENT: "符合条件的地点比较少，可以试着放宽偏好、预算或节奏限制。",
    ErrorCode.NO_FEASIBLE_ROUTE: "在你给出的限制下排不出可行的路线，试着放宽预算、步行量或减少偏好。",
    ErrorCode.NOT_FOUND: "请求的资源不存在。",
    ErrorCode.TRIP_NOT_FOUND: "这个行程不存在或已被清理。",
    ErrorCode.PLACE_NOT_FOUND: "找不到这个地点，它可能已更名或不在当前城市。",
    ErrorCode.SHARE_NOT_FOUND: "这个分享链接不存在，或者作者已经取消了分享。",
    ErrorCode.FORBIDDEN: "你没有权限访问该内容。",
    ErrorCode.RATE_LIMITED: "请求太频繁了，休息一下再试。",
    ErrorCode.ADMIN_REQUIRED: "该页面需要管理员令牌。",
    ErrorCode.PROVIDER_UNAVAILABLE: "外部服务暂时不可用，已切换到降级模式。",
    ErrorCode.PROVIDER_TIMEOUT: "外部服务响应超时，已切换到降级模式。",
    ErrorCode.PROVIDER_INVALID_RESPONSE: "外部服务返回了无法解析的内容，已切换到降级模式。",
    ErrorCode.COST_BREAKER_OPEN: "本次规划的花费已达上限，已用现有数据给出结果。",
    ErrorCode.DB_UNAVAILABLE: "数据库暂时不可用，请稍后重试。",
    ErrorCode.INTERNAL: "服务出了点问题，请重试；若持续出现请把下方编号反馈给我们。",
}

_HTTP_STATUS: dict[ErrorCode, int] = {
    ErrorCode.INVALID_INPUT: 422,
    ErrorCode.UNSUPPORTED_CITY: 422,
    ErrorCode.NO_PLACES_FOR_CITY: 503,
    ErrorCode.CANDIDATES_INSUFFICIENT: 200,  # 不是错误，是"结果受限"
    ErrorCode.NO_FEASIBLE_ROUTE: 200,       # 同上，带解释返回
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.TRIP_NOT_FOUND: 404,
    ErrorCode.PLACE_NOT_FOUND: 404,
    ErrorCode.SHARE_NOT_FOUND: 404,
    ErrorCode.FORBIDDEN: 403,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.ADMIN_REQUIRED: 401,
    ErrorCode.PROVIDER_UNAVAILABLE: 502,
    ErrorCode.PROVIDER_TIMEOUT: 504,
    ErrorCode.PROVIDER_INVALID_RESPONSE: 502,
    ErrorCode.COST_BREAKER_OPEN: 200,
    ErrorCode.DB_UNAVAILABLE: 503,
    ErrorCode.INTERNAL: 500,
}


class AppError(Exception):
    """业务异常。所有面向用户的失败都应使用它（或其子类）。"""

    def __init__(
        self,
        code: ErrorCode,
        message: str | None = None,
        *,
        hint: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.message = message or _DEFAULT_HINTS.get(code, "")
        self.hint = hint or _DEFAULT_HINTS.get(code, "")
        self.context = context or {}
        super().__init__(f"[{code}] {self.message}")

    @property
    def http_status(self) -> int:
        return _HTTP_STATUS.get(self.code, 500)

    def to_payload(self) -> dict[str, Any]:
        return {
            "code": str(self.code),
            "message": self.message,
            "hint": self.hint,
            "context": self.context,
        }


class ProviderError(AppError):
    """外部 Provider 故障。**必须**由调用方捕获并降级，不得直接抛给用户。"""

    def __init__(
        self,
        provider: str,
        operation: str,
        *,
        kind: ErrorCode = ErrorCode.PROVIDER_UNAVAILABLE,
        message: str | None = None,
    ) -> None:
        self.provider = provider
        self.operation = operation
        super().__init__(
            kind,
            message or f"{provider} 的 {operation} 调用失败",
            context={"provider": provider, "operation": operation},
        )


class CostBreakerOpen(AppError):
    """成本熔断触发：必须停止后续外部调用并降级，而不是继续花钱。"""

    def __init__(self, kind: str, spent_cny: float, limit_cny: float) -> None:
        super().__init__(
            ErrorCode.COST_BREAKER_OPEN,
            f"本次规划的 {kind} 成本已达上限（已花 ¥{spent_cny:.4f}，上限 ¥{limit_cny:.4f}）",
            context={"kind": kind, "spent_cny": spent_cny, "limit_cny": limit_cny},
        )
