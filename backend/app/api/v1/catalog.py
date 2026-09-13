"""只读目录 API：把知识库变成"可以在软件里看到的东西"。

为什么需要这组端点（而不是让用户去翻数据库）：
    知识库是 M1 的交付物，但如果它只能通过 psql 查看，那"交付了 3776 个地点"
    就只是一句无法验证的话。这组只读端点让数据可以在浏览器、curl、前端页面里
    被真实检查 —— 数据质量的问题也才可能被用户发现并反馈。

设计约束：
1. **全部只读**：不做任何写入，不需要鉴权，也永远不会产生外部调用（零成本）。
2. **如实暴露不确定性**：`unknown_fields`、`verification_status`、`data_quality_flags`、
   来源 URL 一律返回，不在服务端替调用方决定"要不要显示提示"。
3. **分页有上限**：单页最多 100 条，避免把 3700 个地点一次吐出去。
4. 不存在的城市/地点返回统一错误 envelope（404 + 语义化错误码）。
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import Select, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_scoring_config
from app.core.errors import AppError, ErrorCode
from app.db.models import City, KbVersion, Place, PlaceAlias, PlaceSource, Route, RoutePlace, TravelSource
from app.db.session import get_db
from app.domain.categories import CATEGORY_LABELS, PLACE_CATEGORIES
from app.schemas.catalog import (
    CategoryStat,
    CityOut,
    PageMeta,
    PlaceDetailOut,
    PlaceOut,
    PlacePage,
    PreferenceDimensionOut,
    RouteOut,
    RouteStopOut,
    SourceRef,
)
from app.schemas.common import ApiMeta, ok_envelope

router = APIRouter(tags=["catalog"])

MAX_PAGE_SIZE = 100
SCORE_FIELDS = (
    "popularity",
    "photo",
    "food",
    "culture",
    "night_view",
    "family",
    "couple",
    "walkability",
    "rainy_day",
)


# ── 内部工具 ────────────────────────────────────────────────────────────────


async def _city_by_slug(db: AsyncSession, slug: str) -> City:
    city = (await db.execute(select(City).where(City.slug == slug))).scalar_one_or_none()
    if city is None:
        raise AppError(
            ErrorCode.UNSUPPORTED_CITY,
            f"没有城市 {slug} 的数据",
            hint="目前只有广州（guangzhou）。",
            context={"slug": slug},
        )
    return city


def _iso(value: datetime | date | None) -> str | None:
    return value.isoformat() if value is not None else None


def _scores_of(place: Place) -> dict[str, float]:
    out: dict[str, float] = {}
    for dim in SCORE_FIELDS:
        value = getattr(place, f"{dim}_score", None)
        if value is not None:
            out[dim] = float(value)
    return out


def _score_source(place: Place) -> str:
    """分值的来源：人工校准 / 规则推导。UI 必须能区分这两者。"""
    if "score_curated" in place.data_quality_flags:
        return "curated"
    return "derived"


async def _sources_of(db: AsyncSession, place_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[SourceRef]]:
    """批量取来源（避免 N+1 查询）。"""
    if not place_ids:
        return {}
    rows = (
        await db.execute(
            select(PlaceSource.place_id, PlaceSource.field_scope, TravelSource)
            .join(TravelSource, TravelSource.id == PlaceSource.source_id)
            .where(PlaceSource.place_id.in_(place_ids))
        )
    ).all()
    grouped: dict[uuid.UUID, list[SourceRef]] = {}
    for place_id, field_scope, source in rows:
        grouped.setdefault(place_id, []).append(
            SourceRef(
                name=source.source_name,
                url=source.url,
                source_type=source.source_type,
                credibility=float(source.credibility_score),
                field_scope=list(field_scope or []),
                checked_at=_iso(source.checked_at or source.created_at),
                http_status=source.http_status,
            )
        )
    return grouped


async def _aliases_of(db: AsyncSession, place_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[str]]:
    if not place_ids:
        return {}
    rows = (
        await db.execute(
            select(PlaceAlias.place_id, PlaceAlias.alias).where(PlaceAlias.place_id.in_(place_ids))
        )
    ).all()
    grouped: dict[uuid.UUID, list[str]] = {}
    for place_id, alias in rows:
        grouped.setdefault(place_id, []).append(alias)
    return {key: sorted(value) for key, value in grouped.items()}


def _place_out(
    place: Place,
    *,
    aliases: list[str],
    sources: list[SourceRef],
) -> PlaceOut:
    return PlaceOut(
        id=str(place.id),
        name=place.display_name,
        name_en=place.name_en,
        category=place.category,
        district=place.district,
        latitude=float(place.latitude),
        longitude=float(place.longitude),
        address=place.address,
        recommended_duration_min=place.recommended_duration_min,
        opening_hours_raw=place.opening_hours_raw,
        price_min=float(place.price_min) if place.price_min is not None else None,
        price_max=float(place.price_max) if place.price_max is not None else None,
        indoor=place.indoor,
        best_time=list(place.best_time or []),
        tags=list(place.tags or []),
        aliases=aliases,
        scores=_scores_of(place),
        score_source=_score_source(place),
        verification_status=place.verification_status,
        confidence=float(place.confidence) if place.confidence is not None else None,
        unknown_fields=list(place.unknown_fields or []),
        data_quality_flags=list(place.data_quality_flags or []),
        sources=sources,
    )


# ── 城市 ────────────────────────────────────────────────────────────────────


@router.get("/cities")
async def list_cities(db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """列出可用城市及其数据规模。

    ★ 规模统计用**批量聚合**（固定 3 条查询），不是每个城市各查 3 次 ★
    早期实现是 1 + 3N：城市数从 1 涨到 20，查询数就从 4 涨到 61。
    现在无论多少个城市都是 3 条 —— 有测试钉住"查询次数不随城市数增长"。
    """
    cities = (await db.execute(select(City).order_by(City.name))).scalars().all()

    place_counts = {
        city_id: int(count)
        for city_id, count in (
            await db.execute(
                select(Place.city_id, func.count())
                .where(Place.status == "active")
                .group_by(Place.city_id)
            )
        ).all()
    }
    route_counts = {
        city_id: int(count)
        for city_id, count in (
            await db.execute(select(Route.city_id, func.count()).group_by(Route.city_id))
        ).all()
    }
    # 每个城市"最新一版知识库"。DISTINCT ON 是 PostgreSQL 的惯用法，
    # 一次取回全部城市的最新版本（SQLAlchemy 在 PG 方言下会渲染成 DISTINCT ON）。
    kb_versions = {
        city_id: str(version)
        for city_id, version in (
            await db.execute(
                select(KbVersion.city_id, KbVersion.version)
                .distinct(KbVersion.city_id)
                .order_by(KbVersion.city_id, KbVersion.created_at.desc())
            )
        ).all()
    }

    items: list[CityOut] = [
        CityOut(
            slug=city.slug,
            name=city.name,
            name_en=city.name_en,
            province=city.province,
            description=city.description,
            timezone=city.timezone,
            status=city.status,
            # 没有任何活跃地点的城市必须显示 0，而不是缺字段或 None
            place_count=place_counts.get(city.id, 0),
            route_count=route_counts.get(city.id, 0),
            kb_version=kb_versions.get(city.id),
        )
        for city in cities
    ]
    return ok_envelope({"items": [item.model_dump() for item in items]}, ApiMeta())


@router.get("/cities/{slug}/stats")
async def city_stats(slug: str, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """类别分布与可信度分布 —— 用于一眼判断"这个城市的数据够不够用"。"""
    city = await _city_by_slug(db, slug)
    rows = (
        await db.execute(
            text(
                "SELECT category, count(*) FROM places"
                " WHERE city_id = :c AND status = 'active' GROUP BY category ORDER BY count(*) DESC"
            ),
            {"c": city.id},
        )
    ).all()
    # 这里展示的是**类别分布**，所以统一用类别名（CATEGORY_LABELS）。
    # 曾经把偏好维度的名字（「美食」）混进来，结果同一张表里既有「餐饮」又有「博物馆」，
    # 两套词汇混用。偏好怎么说属于 /meta/scoring-config 的职责，不在这里重复。
    labels = dict(CATEGORY_LABELS)

    verification = (
        await db.execute(
            text(
                "SELECT verification_status, count(*) FROM places"
                " WHERE city_id = :c AND status = 'active' GROUP BY verification_status"
            ),
            {"c": city.id},
        )
    ).all()
    hours_known = (
        await db.execute(
            text(
                "SELECT count(*) FROM places WHERE city_id = :c AND status = 'active'"
                " AND opening_hours_raw IS NOT NULL"
            ),
            {"c": city.id},
        )
    ).scalar_one()
    total = sum(row[1] for row in rows)
    return ok_envelope(
        {
            "city": slug,
            "total_places": int(total),
            "categories": [
                CategoryStat(category=row[0], label=labels.get(row[0], row[0]), count=int(row[1])).model_dump()
                for row in rows
            ],
            "verification": {row[0]: int(row[1]) for row in verification},
            "opening_hours_known": int(hours_known),
            "opening_hours_unknown": int(total - hours_known),
        },
        ApiMeta(),
    )


# ── 地点 ────────────────────────────────────────────────────────────────────


def _place_query(city_id: uuid.UUID, *, q: str | None, category: str | None, district: str | None) -> Select[Any]:
    stmt = select(Place).where(Place.city_id == city_id, Place.status == "active")
    if category:
        stmt = stmt.where(Place.category == category)
    if district:
        stmt = stmt.where(Place.district == district)
    if q:
        pattern = f"%{q}%"
        stmt = stmt.where(
            or_(
                Place.display_name.ilike(pattern),
                Place.canonical_name.ilike(pattern),
                Place.name_en.ilike(pattern),
                Place.id.in_(
                    select(PlaceAlias.place_id).where(
                        PlaceAlias.city_id == city_id, PlaceAlias.alias.ilike(pattern)
                    )
                ),
            )
        )
    return stmt


@router.get("/cities/{slug}/places")
async def list_places(
    slug: str,
    q: str | None = Query(default=None, max_length=60, description="按名称/别名模糊搜索"),
    category: str | None = Query(default=None, description="按类别过滤"),
    district: str | None = Query(default=None, max_length=30),
    limit: int = Query(default=20, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """地点列表。按热度降序，支持名称/别名模糊搜索与类别过滤。"""
    city = await _city_by_slug(db, slug)
    if category and category not in PLACE_CATEGORIES:
        raise AppError(
            ErrorCode.INVALID_INPUT,
            f"未知类别：{category}",
            hint=f"合法类别：{', '.join(PLACE_CATEGORIES)}",
        )

    base = _place_query(city.id, q=q, category=category, district=district)
    total = (
        await db.execute(select(func.count()).select_from(base.subquery()))
    ).scalar_one()
    rows = (
        await db.execute(
            base.order_by(Place.popularity_score.desc().nulls_last(), Place.display_name)
            .limit(limit)
            .offset(offset)
        )
    ).scalars().all()

    ids = [row.id for row in rows]
    aliases = await _aliases_of(db, ids)
    sources = await _sources_of(db, ids)
    items = [
        _place_out(row, aliases=aliases.get(row.id, []), sources=sources.get(row.id, [])) for row in rows
    ]

    page = PlacePage(
        city=slug,
        filters={"q": q, "category": category, "district": district},
        page=PageMeta(
            total=int(total), limit=limit, offset=offset, has_more=offset + len(items) < int(total)
        ),
        items=items,
    )
    return ok_envelope(page.model_dump(), ApiMeta())


@router.get("/places/{place_id}")
async def get_place(place_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """地点详情（含来源、别名、未知字段与质量标记）。"""
    place = (await db.execute(select(Place).where(Place.id == place_id))).scalar_one_or_none()
    if place is None:
        raise AppError(ErrorCode.PLACE_NOT_FOUND, context={"place_id": str(place_id)})

    aliases = await _aliases_of(db, [place.id])
    sources = await _sources_of(db, [place.id])
    summary = _place_out(place, aliases=aliases.get(place.id, []), sources=sources.get(place.id, []))
    detail = PlaceDetailOut(
        **summary.model_dump(),
        canonical_name=place.canonical_name,
        external_source=place.external_source,
        external_id=place.external_id,
        source_name=place.source_name,
        source_url=place.source_url,
        updated_at=_iso(place.updated_at),
    )
    return ok_envelope(detail.model_dump(), ApiMeta())


# ── 路线 ────────────────────────────────────────────────────────────────────


@router.get("/cities/{slug}/routes")
async def list_routes(
    slug: str,
    route_type: str | None = Query(default=None),
    archetype: str | None = Query(default=None, description="relaxed | classic | themed"),
    with_stops: bool = Query(default=True, description="是否返回每站的明细"),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """路线模板列表。`metrics_are_estimated` 恒为 true：时长与步行距离是估算值。"""
    city = await _city_by_slug(db, slug)

    stmt = select(Route).where(Route.city_id == city.id)
    if route_type:
        stmt = stmt.where(Route.route_type == route_type)
    if archetype:
        if archetype not in ("relaxed", "classic", "themed"):
            raise AppError(ErrorCode.INVALID_INPUT, f"未知 archetype：{archetype}")
        stmt = stmt.where(Route.archetype_hint == archetype)
    routes = (await db.execute(stmt.order_by(Route.slug))).scalars().all()

    stops_by_route: dict[uuid.UUID, list[RouteStopOut]] = {}
    if with_stops and routes:
        rows = (
            await db.execute(
                select(RoutePlace, Place)
                .join(Place, Place.id == RoutePlace.place_id)
                .where(RoutePlace.route_id.in_([route.id for route in routes]))
                .order_by(RoutePlace.route_id, RoutePlace.seq)
            )
        ).all()
        for route_place, place in rows:
            stops_by_route.setdefault(route_place.route_id, []).append(
                RouteStopOut(
                    seq=route_place.seq,
                    place_id=str(place.id),
                    place_name=place.display_name,
                    category=place.category,
                    stay_min=route_place.stay_min,
                    note=route_place.note,
                    transport_to_next=route_place.transport_to_next,
                    transport_to_next_min=route_place.transport_to_next_min,
                    distance_to_next_m=route_place.distance_to_next_m,
                )
            )

    items = [
        RouteOut(
            id=str(route.id),
            slug=route.slug,
            name=route.name,
            description=route.description,
            route_type=route.route_type,
            archetype_hint=route.archetype_hint,
            pace=route.pace,
            difficulty=route.difficulty,
            duration_min=route.duration_min,
            walking_distance_m=route.walking_distance_m,
            estimated_transport_time_min=route.estimated_transport_time_min,
            recommended_start_time=route.recommended_start_time.strftime("%H:%M")
            if route.recommended_start_time
            else None,
            recommended_end_time=route.recommended_end_time.strftime("%H:%M")
            if route.recommended_end_time
            else None,
            best_for=list(route.best_for or []),
            stop_count=len(stops_by_route.get(route.id, [])),
            stops=stops_by_route.get(route.id, []),
            metrics_are_estimated=True,
            estimated_budget=None,  # 刻意留空：没有价格来源，不填估算值
            verification_status=route.verification_status,
            source_name=route.source_name,
        ).model_dump()
        for route in routes
    ]
    return ok_envelope(
        {"city": slug, "total": len(items), "items": items},
        ApiMeta(),
    )


# ── 元信息 ──────────────────────────────────────────────────────────────────


@router.get("/meta/scoring-config")
async def scoring_config() -> dict[str, Any]:
    """暴露当前的评分权重与偏好维度（透明性：排序规则不该是黑箱）。"""
    scoring = get_scoring_config()
    return ok_envelope(
        {
            "version": scoring.version,
            "default_weights": scoring.default_weights.model_dump(),
            "archetypes": {
                key: {
                    "label": value.label,
                    "description": value.description,
                    "weights": value.weights.model_dump(),
                    "max_stops": value.max_stops,
                    "walking_cap_ratio": value.walking_cap_ratio,
                }
                for key, value in scoring.archetypes.items()
            },
            "preference_dimensions": [
                PreferenceDimensionOut(
                    key=key,
                    label=dim.label,
                    icon=dim.icon,
                    target=dim.field or f"categories:{','.join(dim.categories)}",
                    aliases=list(dim.aliases),
                ).model_dump()
                for key, dim in scoring.preference_dimensions.items()
            ],
        },
        ApiMeta(),
    )
