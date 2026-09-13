"""规划与行程的请求/响应 Schema（PRD §7.2 / §8.5）。

设计约束：
1. **入参强校验**：``extra="forbid"`` —— 前端多传字段应当立刻报错，
   而不是被静默忽略（静默忽略会让"我明明传了"变成一场排查）。
2. **出参如实暴露不确定性**：``degraded_modes`` / ``feasibility_report`` /
   ``transport_source`` / ``metrics_are_estimated`` 一律返回。不可信的数字
   比没有数字更危险，所以"这是估算值"必须与数字一起出去。
3. ``free_text`` 的长度上限来自 ``config/limits.yaml``，不写死在这里（在服务层校验）。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "BudgetInput",
    "PlanAcceptedOut",
    "PlanRequest",
    "RevisionOut",
    "RevisionRequest",
    "ShareOut",
    "ShareRequest",
    "TripOut",
    "TripRouteOut",
    "TripStopOut",
]


class BudgetInput(BaseModel):
    """预算输入。``amount`` 为 0 或省略表示不限制（PRD FR-01）。"""

    model_config = ConfigDict(extra="forbid")

    amount: Decimal | None = Field(default=None, ge=0)
    scope: Literal["per_person", "total"] = "per_person"


class PlanRequest(BaseModel):
    """创建规划请求。字段与前端 ``lib/api.ts`` 的 ``PlanRequest`` 一一对应。"""

    model_config = ConfigDict(extra="forbid")

    city: str = "guangzhou"
    days: int = Field(default=1, ge=1, le=7)
    people: int = Field(default=2, ge=1, le=20)
    preferences: list[str] = Field(default_factory=list)
    pace: Literal["relaxed", "balanced", "packed"] = "relaxed"
    budget: BudgetInput | None = None
    free_text: str = ""
    # 下面三个是表单的可选细化项（前端暂未提供，接口先留出并校验格式）
    start_time: str | None = None
    end_time: str | None = None
    travel_date: str | None = None


class PlanAcceptedOut(BaseModel):
    """202 响应：请求已受理，进度从 ``stream_url`` 读。"""

    model_config = ConfigDict(extra="forbid")

    request_id: str
    stream_url: str
    status: str = "pending"


class TripStopOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seq: int
    place_id: str
    name: str
    category: str
    latitude: float
    longitude: float
    district: str | None = None
    arrive_time: str
    depart_time: str
    stay_min: int
    transport_mode: str | None = None
    transport_min: int | None = None
    transport_distance_m: int | None = None
    #: amap | osrm | estimated | manual —— 前端据此显示"耗时为估算值"
    transport_source: str | None = None
    why_recommended: str | None = None
    tips: str | None = None
    warnings: list[str] = Field(default_factory=list)
    #: 站点快照（历史行程不随知识库变更漂移，PRD §8.7 R7）
    snapshot: dict[str, Any] = Field(default_factory=dict)


class TripRouteOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    label: str
    archetype: str
    theme: str | None = None
    name: str
    one_liner: str | None = None
    place_count: int
    total_duration_min: int
    walking_distance_m: int | None = None
    transit_time_min: int | None = None
    transit_distance_m: int | None = None
    budget_min: Decimal | None = None
    budget_max: Decimal | None = None
    budget_scope: str | None = None
    #: 费用含有估算值/未知项时前端必须如实标注
    budget_estimated: bool = True
    budget_unknown_items: list[str] = Field(default_factory=list)
    recommend_score: float
    score_breakdown: dict[str, Any] = Field(default_factory=dict)
    feasibility: dict[str, Any] = Field(default_factory=dict)
    best_for: list[str] = Field(default_factory=list)
    highlights: list[str] = Field(default_factory=list)
    pros: list[str] = Field(default_factory=list)
    cons: list[str] = Field(default_factory=list)
    recommendation_reason: str | None = None
    route_source: str | None = None
    template_route_id: str | None = None
    stops: list[TripStopOut] = Field(default_factory=list)


class TripOut(BaseModel):
    """完整行程（PRD §7.2 ``GET /trips/{id}``）。"""

    model_config = ConfigDict(extra="forbid")

    trip_id: str
    request_id: str
    city: str
    title: str | None = None
    days: int
    revision_no: int
    route_count: int
    route_count_requested: int = 3
    #: 少于期望方案数时给出明确原因，而不是凑数（PRD FR-06 / FR-08 AC-8.6）
    route_count_note: str | None = None
    intent: dict[str, Any] = Field(default_factory=dict)
    degraded_modes: list[str] = Field(default_factory=list)
    total_cost_cny: Decimal = Decimal("0")
    generation_ms: int | None = None
    created_at: str | None = None
    is_public: bool = False
    share_slug: str | None = None
    routes: list[TripRouteOut] = Field(default_factory=list)


class RevisionRequest(BaseModel):
    """自然语言修改（PRD FR-08）。"""

    model_config = ConfigDict(extra="forbid")

    instruction: str = Field(min_length=1, max_length=300)


class ShareRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    public: bool = True


class RevisionOut(BaseModel):
    """修改结果：新 revision + Diff 摘要（PRD FR-08 AC-8.4）。"""

    model_config = ConfigDict(extra="forbid")

    trip_id: str
    revision_no: int
    diff: dict[str, Any] = Field(default_factory=dict)
    needs_clarification: str | None = None


class ShareOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str
    url: str
    is_public: bool
