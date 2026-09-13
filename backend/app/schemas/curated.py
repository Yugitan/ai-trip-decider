"""人工整理数据（data/curated/*.yaml）的 Schema。

为什么要给人工数据也建 Schema：它们是最容易写错的一环（例如把不存在的分值维度
写进 scores）。之前的实践里就出现过 ``shopping:`` / ``quiet:`` 这类非法维度，
如果不在加载时校验，错误会一路混进数据库，等到排序结果离谱时才发现。
"""

from __future__ import annotations

from datetime import time
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.config import SCORE_DIMENSIONS
from app.domain.categories import PLACE_CATEGORIES

_ROUTE_TYPES = (
    "classic",
    "food",
    "photo",
    "culture",
    "night_view",
    "family",
    "couple",
    "citywalk",
    "relax",
    "nature",
    "shopping",
    "museum",
)
_TRANSPORTS = ("walk", "metro", "bus", "taxi", "bike", "ferry")
_PACES = ("relaxed", "balanced", "packed")
_ARCHETYPES = ("relaxed", "classic", "themed")


class CuratedPlace(BaseModel):
    """人工地点覆盖：只提供别名与编辑性评分，**不提供任何事实字段**。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)
    category: str | None = None
    recommended_duration_min: int | None = Field(default=None, ge=10, le=720)
    scores: dict[str, float] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    note: str = ""

    @model_validator(mode="after")
    def _valid(self) -> CuratedPlace:
        if self.category is not None and self.category not in PLACE_CATEGORIES:
            raise ValueError(f"地点「{self.name}」的类别 {self.category} 不合法")
        unknown = set(self.scores) - set(SCORE_DIMENSIONS)
        if unknown:
            raise ValueError(f"地点「{self.name}」的 scores 含非法维度：{sorted(unknown)}")
        for dim, value in self.scores.items():
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"地点「{self.name}」的 {dim}={value} 必须在 [0, 1] 内")
        return self


class CuratedPlaceFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str
    city: str
    places: list[CuratedPlace]

    @model_validator(mode="after")
    def _no_duplicate_names(self) -> CuratedPlaceFile:
        seen: set[str] = set()
        for place in self.places:
            key = place.name.strip()
            if key in seen:
                raise ValueError(f"人工地点数据里出现重复名称：{key}")
            seen.add(key)
        return self


class RouteStop(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    stay_min: int | None = Field(default=None, ge=10, le=600)
    note: str = ""
    transport_to_next: str | None = None

    @model_validator(mode="after")
    def _transport(self) -> RouteStop:
        if self.transport_to_next is not None and self.transport_to_next not in _TRANSPORTS:
            raise ValueError(f"站点「{self.name}」的交通方式 {self.transport_to_next} 不合法")
        return self


class CuratedRoute(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    slug: str = Field(min_length=3, pattern=r"^[a-z0-9\-]+$")
    name: str = Field(min_length=2)
    route_type: str
    archetype_hint: str | None = None
    pace: str | None = None
    difficulty: str | None = None
    description: str = ""
    recommended_start_time: time | None = None
    recommended_end_time: time | None = None
    best_for: list[str] = Field(default_factory=list)
    stops: list[RouteStop]

    @model_validator(mode="after")
    def _valid(self) -> CuratedRoute:
        if self.route_type not in _ROUTE_TYPES:
            raise ValueError(f"路线 {self.slug} 的 route_type={self.route_type} 不合法")
        if self.archetype_hint is not None and self.archetype_hint not in _ARCHETYPES:
            raise ValueError(f"路线 {self.slug} 的 archetype_hint={self.archetype_hint} 不合法")
        if self.pace is not None and self.pace not in _PACES:
            raise ValueError(f"路线 {self.slug} 的 pace={self.pace} 不合法")
        if self.difficulty is not None and self.difficulty not in ("easy", "moderate", "hard"):
            raise ValueError(f"路线 {self.slug} 的 difficulty={self.difficulty} 不合法")
        # 少于 3 站的"路线"没有规划价值，属于凑数（PRD §9.4 明确禁止伪路线）
        if len(self.stops) < 3:
            raise ValueError(f"路线 {self.slug} 只有 {len(self.stops)} 个站点，少于 3 站不算路线")
        names = [stop.name for stop in self.stops]
        if len(set(names)) != len(names):
            raise ValueError(f"路线 {self.slug} 的站点有重复：{names}")
        return self


class CuratedRouteFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str
    city: str
    routes: list[CuratedRoute]

    @model_validator(mode="after")
    def _no_duplicate_slugs(self) -> CuratedRouteFile:
        slugs = [route.slug for route in self.routes]
        duplicates = {s for s in slugs if slugs.count(s) > 1}
        if duplicates:
            raise ValueError(f"路线 slug 重复：{sorted(duplicates)}")
        return self


def load_curated_places(path: Any) -> CuratedPlaceFile:
    import yaml

    return CuratedPlaceFile.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def load_curated_routes(path: Any) -> CuratedRouteFile:
    import yaml

    return CuratedRouteFile.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
