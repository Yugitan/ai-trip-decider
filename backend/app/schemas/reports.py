"""客户端上报的 Schema：纠错反馈（PRD FR-11.5）与错误日志（PRD FR-13.5 / §25）。

两条铁律：

1. **长度上限在配置里**（``config/limits.yaml`` 的 ``rate_limit.*_max_chars``），
   不在这里写死 —— 与自由文本（``input_free_text_max_chars``）同一套口径，
   否则"为什么这段文本 500 字被拒、那段 2000 字却进了库"会变成一件要读代码才知道的事。
2. **不信任输入**：``extra="forbid"``；错误日志的 ``context`` 一律脱敏 + 截断
   （PRD §24：不记录 API Key、不记录用户自由文本全文）。这张表是给人看错误现场的，
   不是用户数据的副本。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "FEEDBACK_CATEGORIES",
    "ErrorReportIn",
    "ErrorReportOut",
    "FeedbackCategory",
    "FeedbackIn",
    "FeedbackOut",
]

#: 纠错类别。★ 必须与 ``feedback`` 表的 CHECK 约束逐字一致 ★
#: （``backend/app/db/models.py`` 的 ``ck_feedback_category``）——
#: 两边各写一份是不可避免的（一个在 DDL 里、一个在 Python 类型里），
#: 所以有一条测试专门比对它们，详见 ``tests/unit/test_reports_schema.py``。
FEEDBACK_CATEGORIES = (
    "wrong_hours",
    "wrong_price",
    "closed",
    "bad_route",
    "wrong_coord",
    "other",
)

FeedbackCategory = Literal[
    "wrong_hours",
    "wrong_price",
    "closed",
    "bad_route",
    "wrong_coord",
    "other",
]

ErrorLevel = Literal["debug", "info", "warning", "error", "critical"]


class FeedbackIn(BaseModel):
    """一次纠错上报。``message`` 与 ``contact`` 都是可选的 ——

    强制留言会让人直接离开，而我们真正需要的是"哪一条数据错了"。
    ``trip_id`` / ``place_id`` 至少给一个才有定位价值，但也不强制：
    用户可以只说"夜景那套路线排得不对"。
    """

    model_config = ConfigDict(extra="forbid")

    category: FeedbackCategory
    message: str | None = None
    contact: str | None = Field(default=None, max_length=200)
    place_id: str | None = Field(default=None, max_length=64)
    trip_id: str | None = Field(default=None, max_length=64)


class FeedbackOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    status: str
    #: 如实说明我们会拿它做什么，而不是"感谢反馈"了事。
    note: str


class ErrorReportIn(BaseModel):
    """浏览器上报的一条错误（前端 error boundary 发来的）。

    ``component`` 是唯一必填的描述性字段：没有它，一条错误日志只是一句无主的话。
    """

    model_config = ConfigDict(extra="forbid")

    component: str = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1)
    level: ErrorLevel = "error"
    code: str | None = Field(default=None, max_length=64)
    request_id: str | None = Field(default=None, max_length=64)
    context: dict[str, Any] | None = None


class ErrorReportOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    #: 我们**不**回显 message/context：回显等于把不可信输入再送回浏览器一遍，
    #: 唯一需要确认的是"收到了"（以及它的行号，便于对照服务端日志）。
    recorded: bool = True
