"""只读目录（catalog）的响应 Schema。

设计原则：**把数据的不确定性原样暴露给调用方**，而不是替它抹平。
- 每个地点都带 `verification_status`、`confidence`、`unknown_fields`、`data_quality_flags`
  与来源 URL —— 调用方（前端）据此决定要不要显示"出发前请确认"。
- 分值字段带 `score_source`，明确它是**编辑性评分**而不是事实。
- 不提供任何"看起来完整"的默认值：没有的字段就是 null。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class SourceRef(BaseModel):
    """一条来源引用（地点 → 来源，含该来源支持哪些字段）。"""

    model_config = ConfigDict(extra="forbid")

    name: str
    url: str | None = None
    source_type: str
    credibility: float
    field_scope: list[str] = Field(default_factory=list)
    checked_at: str | None = None
    http_status: int | None = None


class CityOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str
    name: str
    name_en: str | None = None
    province: str | None = None
    description: str | None = None
    timezone: str
    status: str
    place_count: int
    route_count: int
    kb_version: str | None = None


class PlaceOut(BaseModel):
    """地点摘要（列表用）。"""

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    name_en: str | None = None
    category: str
    district: str | None = None
    latitude: float
    longitude: float
    address: str | None = None

    recommended_duration_min: int | None = None
    opening_hours_raw: str | None = None
    price_min: float | None = None
    price_max: float | None = None
    indoor: bool | None = None
    best_time: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    aliases: list[str] = Field(default_factory=list)

    # 分值：编辑性排序量，不是事实
    scores: dict[str, float] = Field(default_factory=dict)
    score_source: str

    # 可信度：如实暴露
    verification_status: str
    confidence: float | None = None
    unknown_fields: list[str] = Field(default_factory=list)
    data_quality_flags: list[str] = Field(default_factory=list)
    sources: list[SourceRef] = Field(default_factory=list)


class PlaceDetailOut(PlaceOut):
    """地点详情（多带原始 OSM 标签与外部 ID，便于追溯）。"""

    model_config = ConfigDict(extra="forbid")

    canonical_name: str
    external_source: str | None = None
    external_id: str | None = None
    source_name: str | None = None
    source_url: str | None = None
    updated_at: str | None = None


class RouteStopOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seq: int
    place_id: str
    place_name: str
    category: str
    stay_min: int | None = None
    note: str | None = None
    transport_to_next: str | None = None
    transport_to_next_min: int | None = None
    distance_to_next_m: int | None = None


class RouteOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    slug: str
    name: str
    description: str | None = None
    route_type: str
    archetype_hint: str | None = None
    pace: str | None = None
    difficulty: str | None = None
    duration_min: int | None = None
    walking_distance_m: int | None = None
    estimated_transport_time_min: int | None = None
    recommended_start_time: str | None = None
    recommended_end_time: str | None = None
    best_for: list[str] = Field(default_factory=list)
    stop_count: int = 0
    stops: list[RouteStopOut] = Field(default_factory=list)
    # 路线指标是估算值：调用方必须原样展示这个事实
    metrics_are_estimated: bool = True
    estimated_budget: dict[str, Any] | None = None
    verification_status: str
    source_name: str | None = None


class PageMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total: int
    limit: int
    offset: int
    has_more: bool


class PlacePage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    city: str
    filters: dict[str, Any] = Field(default_factory=dict)
    page: PageMeta
    items: list[PlaceOut]


class PlaceSearchOut(BaseModel):
    """地点搜索的结果（PRD §7.2 ``GET /places/search``）。

    与 ``PlacePage`` 一样带分页元信息，但**带上游的搜索词**：
    调用方要能看出"我搜的是 A，回来的这条是 A 的别名命中"，
    否则一次别名命中看起来就像"它怎么出现了"。
    """

    model_config = ConfigDict(extra="forbid")

    query: str
    city: str | None = None
    page: PageMeta
    items: list[PlaceOut]


class CategoryStat(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: str
    label: str
    count: int


class PreferenceDimensionOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    label: str
    icon: str = ""
    target: str
    aliases: list[str] = Field(default_factory=list)
