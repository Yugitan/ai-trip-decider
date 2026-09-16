"""L1–L4 检索层：把本地知识库接进 8 级瀑布（PRD §15.1）。

为什么单独一个模块：
    引擎（``RetrievalChain``）刻意不认识数据库 —— 它只认 ``resolve()`` 契约。
    L5–L8 在 ``retrieval_layers.py``，L1–L4 是**本地表查询**，放在这里由编排层装配。
    这样"数据从哪来"与"按什么顺序降级"两件事互不纠缠。

三层各自解决一个**不同的信息需求**（不是同一个查询的多个备胎）：
    L1 ``places``          —— 这座城市有哪些可玩的地点（规划的主素材）
    L3 ``route_templates`` —— 人工整理的路线模板（命中即复用，成本为 0）
    L4 ``relations``       —— 地点两两之间的距离/耗时/组合质量

★ 刻意的缺口：L2（热门地点库）没有实现 ★
    PRD §15.1 把 L2 列为"热门地点库"，但在当前数据模型里它**不是一份独立数据**：
    `places` 表里就有 `popularity_score`，L1 查的就是这张表。
    为了"层数完整"再查一遍同一张表、把同一个结果集截短，只会：
    （1）每次规划多一次数据库往返；（2）让报表看起来覆盖了 L2，实际上什么都没多查。
    热度确实参与规划，但它在 ``candidates.select_candidates`` 的
    ``0.4 × popularity`` 里参与 —— 那是算法的一部分，不是一个检索层。
    若将来热门地点变成独立数据（例如运营榜单、季节活动），再在这里补上真实实现。

★ 诚实性红线 ★
    营业时间由 ``app/domain/opening_hours.py`` 从 OSM 原文（``opening_hours_raw``）
    即时解析（TASKS.md B6）。解析器**看不懂就返回 None**，这时领域层会报
    HOURS_UNKNOWN（"出发前请确认"），而不是假装"24 小时开放"，也不会给它
    编一个看起来精确的时间。库里 509 条原文能解出 267 条（52.5%），
    剩下的多半是跨零点（``05:56-00:25``）或脏数据（``06:06-24:08``）—— 如实留白。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import ScoringConfig
from app.core.errors import AppError, ErrorCode
from app.db.models import City, KbVersion, Place, PlaceRelation, Route, RoutePlace
from app.domain.models import Place as DomainPlace
from app.domain.models import Relation, TransportMode, TransportSource
from app.domain.opening_hours import parse_opening_hours
from app.services.retrieval_chain import FunctionLayer, RetrievalQuery, RetrievalResult

__all__ = [
    "KIND_PLACES",
    "KIND_RELATIONS",
    "KIND_ROUTE_TEMPLATES",
    "TemplateRoute",
    "city_places_layer",
    "count_active_places",
    "latest_kb_version",
    "load_city",
    "relations_layer",
    "route_templates_layer",
    "to_domain_place",
    "to_domain_relation",
]

KIND_PLACES = "places"
KIND_ROUTE_TEMPLATES = "route_templates"
KIND_RELATIONS = "relations"

#: 模板匹配时只取这么多条（人工整理的模板库本身不大，但不需要全量加载）
_TEMPLATE_FETCH_LIMIT = 60


@dataclass(frozen=True, slots=True)
class TemplateRoute:
    """一条人工整理的路线模板（PRD §8.3）。"""

    route_id: str
    slug: str
    name: str
    archetype: str | None
    place_ids: tuple[str, ...]
    stay_min: tuple[int | None, ...]
    duration_min: int | None

    @property
    def stop_count(self) -> int:
        return len(self.place_ids)


# ════════════════════════════════════════════════════════════════════════════
# ORM → 领域对象（映射只在这里发生，算法永远看不到 SQLAlchemy）
# ════════════════════════════════════════════════════════════════════════════


async def load_city(db: AsyncSession, slug: str) -> City:
    """按 slug 取城市。找不到就抛 ``UNSUPPORTED_CITY``（而不是返回空结果）。"""
    city = (await db.execute(select(City).where(City.slug == slug))).scalar_one_or_none()
    if city is None:
        raise AppError(
            ErrorCode.UNSUPPORTED_CITY,
            f"没有城市 {slug} 的数据",
            hint="目前只有广州（guangzhou）。",
            context={"slug": slug},
        )
    return city


async def latest_kb_version(db: AsyncSession, city_id: uuid.UUID) -> str:
    """知识库版本号。它是规划缓存键的一部分：建库后旧缓存**自动失效**。

    没有版本记录时返回 ``"unknown"`` 而不是空串 —— 空串会让两个不同城市、
    甚至"没有版本"与"版本为空"生成同一个缓存键。
    """
    version = (
        await db.execute(
            select(KbVersion.version)
            .where(KbVersion.city_id == city_id)
            .order_by(KbVersion.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return str(version) if version else "unknown"


def to_domain_place(row: Place, scoring: ScoringConfig) -> DomainPlace:
    """把地点行映射成领域值对象。

    分值列的映射依据**配置**（``scoring.preference_dimensions[*].field``），
    而不是硬编码一长串列名 —— 加一个偏好维度只需要改 YAML。
    缺失（列 NULL）不填 0：``dimension_score`` 会把"不知道"按配置的中性值处理，
    而 0 会被当成"确定地不适合"。
    """
    scores: dict[str, float] = {}
    for key, dimension in scoring.preference_dimensions.items():
        field = dimension.field
        if not field:
            continue  # 按类别匹配的维度（nature/shopping/museum）没有分值列
        value = getattr(row, field, None)
        if value is not None:
            scores[key] = float(value)

    return DomainPlace(
        id=str(row.id),
        name=row.display_name,
        category=row.category,
        lat=float(row.latitude),
        lng=float(row.longitude),
        district=row.district,
        recommended_duration_min=row.recommended_duration_min,
        popularity_score=float(row.popularity_score) if row.popularity_score is not None else None,
        scores=scores,
        price_min=_money(row.price_min),
        price_max=_money(row.price_max),
        # 营业时间：由 OSM 原文（`opening_hours_raw`）**即时解析**成结构化时段。
        # 解析器只做能保证正确的那一部分，看不懂就是 None → 仍然"未知"，
        # 前端会带上一句"出发前请确认"（一个猜错的开放时间比未知更危险）。
        # 为什么不落库到 `opening_hours` 列：解析只有一份实现，原文才是权威；
        # 3166 个地点全量解析实测 1.4ms，不值得为它多一条会漂的副本。
        opening_hours=parse_opening_hours(row.opening_hours_raw),
        indoor=row.indoor,
        rainy_day_score=float(row.rainy_day_score) if row.rainy_day_score is not None else None,
        tags=tuple(row.tags or ()),
        verification_status=row.verification_status,
        status=row.status,
    )


def to_domain_relation(row: PlaceRelation) -> Relation:
    """把关系行映射成领域关系。

    ``transit_time_min`` 只映射到 ``metro``：广州的公共交通主力是地铁，
    而公交（bus）的耗时与地铁不同，用同一个数字冒充两种方式是编造。
    bus 缺失时会自动回退到"距离 × 速度假设"并标记 estimated（见 ``leg_between``）。
    """
    minutes: dict[TransportMode, int] = {}
    if row.walking_time_min is not None:
        minutes["walk"] = int(row.walking_time_min)
    if row.cycling_time_min is not None:
        minutes["bike"] = int(row.cycling_time_min)
    if row.transit_time_min is not None:
        minutes["metro"] = int(row.transit_time_min)
    if row.driving_time_min is not None:
        minutes["taxi"] = int(row.driving_time_min)

    source: TransportSource = row.data_source  # type: ignore[assignment]
    return Relation(
        a_id=str(row.place_a_id),
        b_id=str(row.place_b_id),
        distance_m=int(row.distance_m) if row.distance_m is not None else None,
        minutes_by_mode=minutes,
        relationship_score=(
            float(row.relationship_score) if row.relationship_score is not None else None
        ),
        derived_modes=tuple(row.derived_modes or ()),
        source=source,
    )


def _money(value: Decimal | None) -> Decimal | None:
    return None if value is None else Decimal(value)


# ════════════════════════════════════════════════════════════════════════════
# 四层
# ════════════════════════════════════════════════════════════════════════════


def city_places_layer(db: AsyncSession, city_id: uuid.UUID, scoring: ScoringConfig) -> FunctionLayer:
    """L1：城市知识库 —— 该城市全部 active 地点。

    只取 active：``merged_into_place_id`` 非空或被标为 closed 的地点不该出现在
    任何方案里（合并后的记录仍留在表里是为了可追溯，不是为了被推荐）。
    """

    async def _resolve(query: RetrievalQuery) -> RetrievalResult | None:
        rows = (
            await db.execute(
                select(Place).where(Place.city_id == city_id, Place.status == "active")
            )
        ).scalars().all()
        return RetrievalResult(
            value=[to_domain_place(row, scoring) for row in rows],
            resolved_by="L1",
        )

    return FunctionLayer("L1", _resolve, kinds=[KIND_PLACES])


def route_templates_layer(db: AsyncSession, city_id: uuid.UUID) -> FunctionLayer:
    """L3：人工整理的路线模板。命中即可直接复用（成本为 0，见 PRD §15.1）。"""

    async def _resolve(query: RetrievalQuery) -> RetrievalResult | None:
        archetype = query.payload.get("archetype")
        stmt = select(Route).where(Route.city_id == city_id, Route.is_template)
        if isinstance(archetype, str) and archetype:
            stmt = stmt.where(Route.archetype_hint == archetype)
        routes = (
            await db.execute(stmt.order_by(Route.slug).limit(_TEMPLATE_FETCH_LIMIT))
        ).scalars().all()
        if not routes:
            return RetrievalResult(value=[], resolved_by="L3")

        rows = (
            await db.execute(
                select(RoutePlace.route_id, RoutePlace.place_id, RoutePlace.stay_min)
                .where(RoutePlace.route_id.in_([route.id for route in routes]))
                .order_by(RoutePlace.route_id, RoutePlace.seq)
            )
        ).all()
        grouped: dict[uuid.UUID, list[tuple[str, int | None]]] = {}
        for route_id, place_id, stay_min in rows:
            grouped.setdefault(route_id, []).append((str(place_id), stay_min))

        templates = [
            TemplateRoute(
                route_id=str(route.id),
                slug=route.slug,
                name=route.name,
                archetype=route.archetype_hint,
                place_ids=tuple(pid for pid, _ in grouped.get(route.id, [])),
                stay_min=tuple(stay for _, stay in grouped.get(route.id, [])),
                duration_min=route.duration_min,
            )
            for route in routes
        ]
        # 只有 >= 3 站的模板才值得复用（2 站更像"打车去一个地方"，见 limits.min_route_stops）
        return RetrievalResult(
            value=[template for template in templates if template.stop_count >= 3],
            resolved_by="L3",
        )

    return FunctionLayer("L3", _resolve, kinds=[KIND_ROUTE_TEMPLATES])


def relations_layer(db: AsyncSession, city_id: uuid.UUID) -> FunctionLayer:
    """L4：地点关系数据（缓存图）。只取与本次候选相关的那些对。"""

    async def _resolve(query: RetrievalQuery) -> RetrievalResult | None:
        raw_ids = query.payload.get("place_ids")
        if not isinstance(raw_ids, Sequence) or isinstance(raw_ids, str | bytes):
            return None  # 形状不对就是"本层不处理"，交给上层决定（而不是猜）
        try:
            place_ids = [uuid.UUID(str(pid)) for pid in raw_ids]
        except ValueError:
            return None
        if not place_ids:
            return RetrievalResult(value=[], resolved_by="L4")
        rows = (
            await db.execute(
                select(PlaceRelation).where(
                    PlaceRelation.city_id == city_id,
                    PlaceRelation.place_a_id.in_(place_ids),
                    PlaceRelation.place_b_id.in_(place_ids),
                )
            )
        ).scalars().all()
        return RetrievalResult(value=[to_domain_relation(row) for row in rows], resolved_by="L4")

    return FunctionLayer("L4", _resolve, kinds=[KIND_RELATIONS])


async def count_active_places(db: AsyncSession, city_id: uuid.UUID) -> int:
    """城市可用地点数。用于"知识库为空"的明确报错（而不是生成 0 套方案）。"""
    return int(
        (
            await db.execute(
                select(func.count())
                .select_from(Place)
                .where(Place.city_id == city_id, Place.status == "active")
            )
        ).scalar_one()
    )
