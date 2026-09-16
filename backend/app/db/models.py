"""全部 ORM 模型（对应 PRD §8 数据模型）。

命名约定：
- 表名复数蛇形；列名蛇形；金额列以 ``_cny`` 结尾的不是必须，但语义必须清楚。
- 所有"来源"字段与 `verification_status` 必须成对出现（PRD §8.7 R3）。
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    Index,
    Integer,
    Text,
    Time,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import (
    Base,
    amount6,
    bigint_pk,
    created_at_col,
    jsonb,
    jsonb_required,
    money,
    score,
    text_array,
    ts,
    updated_at_col,
    uuid_fk,
    uuid_pk,
)
from app.domain.categories import PLACE_CATEGORIES as _PLACE_CATEGORY_VALUES

# ════════════════════════════════════════════════════════════════════════════
# 城市与地点
# ════════════════════════════════════════════════════════════════════════════

CITY_STATUS = ("draft", "seeding", "active", "archived")

VERIFICATION_STATUS = ("verified", "probable", "unknown", "stale", "conflicting")

PLACE_STATUS = ("active", "candidate", "merged", "archived")


class City(Base):
    __tablename__ = "cities"

    id: Mapped[uuid.UUID] = uuid_pk()
    slug: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    name_en: Mapped[str | None] = mapped_column(Text)
    country: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'CN'"))
    province: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    timezone: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'Asia/Shanghai'"))
    currency: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'CNY'"))

    centroid_lat: Mapped[float | None] = mapped_column(Float)
    centroid_lng: Mapped[float | None] = mapped_column(Float)
    bbox_min_lat: Mapped[float | None] = mapped_column(Float)
    bbox_max_lat: Mapped[float | None] = mapped_column(Float)
    bbox_min_lng: Mapped[float | None] = mapped_column(Float)
    bbox_max_lng: Mapped[float | None] = mapped_column(Float)

    coverage_score: Mapped[float | None] = score()
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'draft'"))
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()

    __table_args__ = (
        CheckConstraint(f"status IN {CITY_STATUS}", name="ck_cities_status"),
    )


class Place(Base):
    __tablename__ = "places"

    id: Mapped[uuid.UUID] = uuid_pk()
    city_id: Mapped[uuid.UUID] = uuid_fk("cities.id")

    canonical_name: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    name_en: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str] = mapped_column(Text, nullable=False)
    subcategory: Mapped[str | None] = mapped_column(Text)
    district: Mapped[str | None] = mapped_column(Text)

    latitude: Mapped[float] = mapped_column(Float, nullable=False)
    longitude: Mapped[float] = mapped_column(Float, nullable=False)
    geohash: Mapped[str | None] = mapped_column(Text)
    address: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)

    recommended_duration_min: Mapped[int | None] = mapped_column(Integer)
    opening_hours: Mapped[dict[str, Any] | None] = jsonb()
    opening_hours_raw: Mapped[str | None] = mapped_column(Text)

    price_min: Mapped[Decimal | None] = money()
    price_max: Mapped[Decimal | None] = money()
    price_note: Mapped[str | None] = mapped_column(Text)

    best_time: Mapped[list[str]] = text_array()
    best_season: Mapped[list[str]] = text_array()
    indoor: Mapped[bool | None] = mapped_column(Boolean)
    reservation_required: Mapped[bool | None] = mapped_column(Boolean)
    crowd_level: Mapped[str | None] = mapped_column(Text)

    # 0..1 启发式分值（不是事实，见 config/seed.yaml 的诚实性声明）
    popularity_score: Mapped[float | None] = score()
    photo_score: Mapped[float | None] = score()
    food_score: Mapped[float | None] = score()
    culture_score: Mapped[float | None] = score()
    night_view_score: Mapped[float | None] = score()
    family_score: Mapped[float | None] = score()
    couple_score: Mapped[float | None] = score()
    walkability_score: Mapped[float | None] = score()
    rainy_day_score: Mapped[float | None] = score()
    quiet_score: Mapped[float | None] = score()
    seat_score: Mapped[float | None] = score()

    tags: Mapped[list[str]] = text_array()

    primary_source_id: Mapped[uuid.UUID | None] = uuid_fk(
        "travel_sources.id", ondelete="SET NULL", nullable=True
    )
    source_url: Mapped[str | None] = mapped_column(Text)
    source_name: Mapped[str | None] = mapped_column(Text)
    source_updated_at: Mapped[datetime | None] = ts()
    verification_status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'unknown'")
    )
    confidence: Mapped[float | None] = score()
    last_verified_at: Mapped[datetime | None] = ts()
    unknown_fields: Mapped[list[str]] = text_array()
    data_quality_flags: Mapped[list[str]] = text_array()

    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'active'"))
    merged_into_place_id: Mapped[uuid.UUID | None] = uuid_fk(
        "places.id", ondelete="SET NULL", nullable=True
    )

    # 数据来源追溯（M1 用；便于质检脚本判定"是否来自 OSM"）
    external_source: Mapped[str | None] = mapped_column(Text)
    external_id: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()

    __table_args__ = (
        CheckConstraint(f"category IN {_PLACE_CATEGORY_VALUES}", name="ck_places_category"),
        CheckConstraint(f"verification_status IN {VERIFICATION_STATUS}", name="ck_places_verification"),
        CheckConstraint(f"status IN {PLACE_STATUS}", name="ck_places_status"),
        CheckConstraint(
            "recommended_duration_min IS NULL OR "
            "(recommended_duration_min >= 10 AND recommended_duration_min <= 720)",
            name="ck_places_duration_range",
        ),
        CheckConstraint(
            "price_min IS NULL OR price_max IS NULL OR price_min <= price_max",
            name="ck_places_price_order",
        ),
        CheckConstraint("latitude <> 0 AND longitude <> 0", name="ck_places_coord_not_zero"),
        CheckConstraint("merged_into_place_id IS NULL OR merged_into_place_id <> id",
                        name="ck_places_merge_not_self"),
        # 规则 R3：verified 必须有 source_url（在 DB 层强制，配合质检脚本）
        CheckConstraint(
            "verification_status <> 'verified' OR source_url IS NOT NULL",
            name="ck_places_verified_needs_source",
        ),
        # ★ 刻意不是唯一索引 ★
        # 早期版本把 (city_id, lower(canonical_name)) 设成唯一，结果被真实数据立刻打脸：
        # 广州有 9 个"中山公园"、9 个"图书馆"、不止一处"广东美术馆" —— 它们是**不同的地点**。
        # 实体身份的正确判据是「同名 + 地理邻近」（见 app/domain/naming.py 与 PRD §14.2 的地理围栏），
        # 而不是"名称全局唯一"。这个索引只用于加速查找，不承担唯一性职责。
        Index("ix_places_city_canonical", "city_id", text("lower(canonical_name)")),
        Index("ix_places_city_status_cat", "city_id", "status", "category"),
        Index("ix_places_tags", "tags", postgresql_using="gin"),
        Index("ix_places_bbox", "city_id", "latitude", "longitude"),
        Index("ix_places_geohash", "geohash"),
        # 中文模糊匹配：用 pg_trgm 的 gin_trgm_ops 操作符类（放在 postgresql_ops 里
        # 而不是写成表达式，这样 alembic autogenerate 才能正确比较该索引）
        Index(
            "ix_places_name_trgm",
            "display_name",
            postgresql_using="gin",
            postgresql_ops={"display_name": "gin_trgm_ops"},
        ),
        Index("ix_places_popularity", "city_id", text("popularity_score DESC NULLS LAST")),
        Index("ix_places_external", "external_source", "external_id"),
    )


class PlaceAlias(Base):
    __tablename__ = "place_aliases"

    id: Mapped[uuid.UUID] = uuid_pk()
    place_id: Mapped[uuid.UUID] = uuid_fk("places.id")
    city_id: Mapped[uuid.UUID] = uuid_fk("cities.id")
    alias: Mapped[str] = mapped_column(Text, nullable=False)
    alias_norm: Mapped[str] = mapped_column(Text, nullable=False)
    alias_type: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float | None] = score()
    created_at: Mapped[datetime] = created_at_col()

    __table_args__ = (
        CheckConstraint(
            "alias_type IN ('zh_variant','en','abbr','historic','common_misspell','poi_name','search_term')",
            name="ck_alias_type",
        ),
        # 同理：同一个别名文本可能指向多个不同地点（"中山公园"），
        # 因此唯一性只能约束在"单个地点内部不重复"，跨地点允许同名。
        UniqueConstraint("place_id", "alias_norm", name="uq_alias_place_norm"),
        Index("ix_alias_place", "place_id"),
        Index("ix_alias_city_norm", "city_id", "alias_norm"),
    )


class PlaceExternalId(Base):
    __tablename__ = "place_external_ids"

    id: Mapped[uuid.UUID] = uuid_pk()
    place_id: Mapped[uuid.UUID] = uuid_fk("places.id")
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    external_id: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_col()

    __table_args__ = (
        UniqueConstraint("provider", "external_id", name="uq_external_provider_id"),
        Index("ix_external_place", "place_id"),
    )


class TravelSource(Base):
    __tablename__ = "travel_sources"

    id: Mapped[uuid.UUID] = uuid_pk()
    source_type: Mapped[str] = mapped_column(Text, nullable=False)
    source_name: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str | None] = mapped_column(Text)
    url_hash: Mapped[str | None] = mapped_column(Text)
    domain: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    language: Mapped[str | None] = mapped_column(Text, server_default=text("'zh'"))
    published_at: Mapped[datetime | None] = ts()
    fetched_at: Mapped[datetime | None] = ts()
    http_status: Mapped[int | None] = mapped_column(Integer)
    robots_allowed: Mapped[bool | None] = mapped_column(Boolean)
    content_hash: Mapped[str | None] = mapped_column(Text)
    extracted_facts: Mapped[dict[str, Any] | None] = jsonb()
    credibility_score: Mapped[float] = mapped_column(Float, nullable=False, server_default=text("0.5"))
    checked_at: Mapped[datetime | None] = ts()
    created_at: Mapped[datetime] = created_at_col()

    __table_args__ = (
        CheckConstraint(
            "source_type IN ('official','gov','tourism_bureau','ota','media','blog','ugc',"
            "'map_api','search_api','llm_inference','manual','editorial')",
            name="ck_source_type",
        ),
        CheckConstraint("credibility_score >= 0 AND credibility_score <= 1", name="ck_source_credibility"),
        Index("uq_sources_url_hash", "url_hash", unique=True, postgresql_where=text("url_hash IS NOT NULL")),
        Index("ix_sources_cred", text("credibility_score DESC")),
    )


class PlaceSource(Base):
    __tablename__ = "place_sources"

    id: Mapped[uuid.UUID] = uuid_pk()
    place_id: Mapped[uuid.UUID] = uuid_fk("places.id")
    source_id: Mapped[uuid.UUID] = uuid_fk("travel_sources.id")
    # 该来源支持哪些字段（字段级溯源，避免"来源只证明名字却用来证明营业时间"）
    field_scope: Mapped[list[str]] = text_array()
    created_at: Mapped[datetime] = created_at_col()

    __table_args__ = (UniqueConstraint("place_id", "source_id", name="uq_place_source"),)


# ════════════════════════════════════════════════════════════════════════════
# 路线模板
# ════════════════════════════════════════════════════════════════════════════

ROUTE_TYPES = (
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


class Route(Base):
    __tablename__ = "routes"

    id: Mapped[uuid.UUID] = uuid_pk()
    city_id: Mapped[uuid.UUID] = uuid_fk("cities.id")
    slug: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    route_type: Mapped[str] = mapped_column(Text, nullable=False)
    archetype_hint: Mapped[str | None] = mapped_column(Text)
    difficulty: Mapped[str | None] = mapped_column(Text)
    pace: Mapped[str | None] = mapped_column(Text)

    duration_min: Mapped[int | None] = mapped_column(Integer)
    walking_distance_m: Mapped[int | None] = mapped_column(Integer)
    estimated_transport_time_min: Mapped[int | None] = mapped_column(Integer)
    estimated_budget_min: Mapped[Decimal | None] = money()
    estimated_budget_max: Mapped[Decimal | None] = money()

    recommended_start_time: Mapped[time | None] = mapped_column(Time)
    recommended_end_time: Mapped[time | None] = mapped_column(Time)
    best_for: Mapped[list[str]] = text_array()
    score: Mapped[float | None] = score()

    is_template: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    usage_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    source_url: Mapped[str | None] = mapped_column(Text)
    source_name: Mapped[str | None] = mapped_column(Text)
    verification_status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'unknown'")
    )
    updated_at: Mapped[datetime] = updated_at_col()
    created_at: Mapped[datetime] = created_at_col()

    __table_args__ = (
        CheckConstraint(f"route_type IN {ROUTE_TYPES}", name="ck_routes_type"),
        CheckConstraint(
            "archetype_hint IS NULL OR archetype_hint IN ('relaxed','classic','themed')",
            name="ck_routes_archetype",
        ),
        CheckConstraint(
            "difficulty IS NULL OR difficulty IN ('easy','moderate','hard')", name="ck_routes_difficulty"
        ),
        CheckConstraint("pace IS NULL OR pace IN ('relaxed','balanced','packed')", name="ck_routes_pace"),
        UniqueConstraint("city_id", "slug", name="uq_routes_city_slug"),
        Index("ix_routes_city_type", "city_id", "route_type"),
    )


class RoutePlace(Base):
    __tablename__ = "route_places"

    id: Mapped[uuid.UUID] = uuid_pk()
    route_id: Mapped[uuid.UUID] = uuid_fk("routes.id")
    place_id: Mapped[uuid.UUID] = uuid_fk("places.id")
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    stay_min: Mapped[int | None] = mapped_column(Integer)
    note: Mapped[str | None] = mapped_column(Text)
    transport_to_next: Mapped[str | None] = mapped_column(Text)
    transport_to_next_min: Mapped[int | None] = mapped_column(Integer)
    distance_to_next_m: Mapped[int | None] = mapped_column(Integer)

    __table_args__ = (
        UniqueConstraint("route_id", "seq", name="uq_route_places_seq"),
        CheckConstraint(
            "transport_to_next IS NULL OR "
            "transport_to_next IN ('walk','metro','bus','taxi','bike','ferry')",
            name="ck_route_places_transport",
        ),
        Index("ix_route_places_place", "place_id"),
    )


class PlaceRelation(Base):
    __tablename__ = "place_relations"

    id: Mapped[uuid.UUID] = uuid_pk()
    city_id: Mapped[uuid.UUID] = uuid_fk("cities.id")
    place_a_id: Mapped[uuid.UUID] = uuid_fk("places.id")
    place_b_id: Mapped[uuid.UUID] = uuid_fk("places.id")

    distance_m: Mapped[int | None] = mapped_column(Integer)
    walking_time_min: Mapped[int | None] = mapped_column(Integer)
    driving_time_min: Mapped[int | None] = mapped_column(Integer)
    transit_time_min: Mapped[int | None] = mapped_column(Integer)
    cycling_time_min: Mapped[int | None] = mapped_column(Integer)

    relationship_score: Mapped[float | None] = score()
    recommended_transport: Mapped[str | None] = mapped_column(Text)
    recommended_together: Mapped[bool | None] = mapped_column(Boolean)
    reason: Mapped[str | None] = mapped_column(Text)
    geometry_ref: Mapped[str | None] = mapped_column(Text)

    data_source: Mapped[str] = mapped_column(Text, nullable=False)
    # 哪些出行方式的耗时是"由距离推导"而不是实测的。
    # 为什么必须有这一列：实测公共 OSRM 只部署了 car profile，请求 foot 也返回车速，
    # 所以步行/公交耗时一律是"真实路网距离 × 文档化速度假设"，必须可区分于真实耗时。
    # 取值示例：{'walk','metro','taxi'}；空数组表示全部来自真实服务。
    derived_modes: Mapped[list[str]] = text_array()
    computed_at: Mapped[datetime] = created_at_col()
    expires_at: Mapped[datetime | None] = ts()

    __table_args__ = (
        CheckConstraint("place_a_id < place_b_id", name="ck_relation_order"),
        CheckConstraint(
            "data_source IN ('amap','osrm','estimated','manual')", name="ck_relation_source"
        ),
        UniqueConstraint("place_a_id", "place_b_id", name="uq_relation_pair"),
        Index("ix_rel_a", "place_a_id"),
        Index("ix_rel_b", "place_b_id"),
        Index("ix_rel_expires", "expires_at"),
    )


class MergeLog(Base):
    """实体合并审计（PRD §14.3）：所有合并可回滚。"""

    __tablename__ = "merge_logs"

    id: Mapped[uuid.UUID] = uuid_pk()
    keep_place_id: Mapped[uuid.UUID] = uuid_fk("places.id")
    drop_place_id: Mapped[uuid.UUID] = uuid_fk("places.id")
    match_level: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float | None] = score()
    reason: Mapped[str | None] = mapped_column(Text)
    snapshot: Mapped[dict[str, Any] | None] = jsonb()
    reverted_at: Mapped[datetime | None] = ts()
    created_at: Mapped[datetime] = created_at_col()

    __table_args__ = (Index("ix_merge_keep", "keep_place_id"),)


# ════════════════════════════════════════════════════════════════════════════
# 规划结果
# ════════════════════════════════════════════════════════════════════════════


class TripRequest(Base):
    __tablename__ = "trip_requests"

    id: Mapped[uuid.UUID] = uuid_pk()
    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    raw_input: Mapped[dict[str, Any]] = jsonb_required()
    free_text_len: Mapped[int | None] = mapped_column(Integer)
    free_text_masked: Mapped[str | None] = mapped_column(Text)
    intent: Mapped[dict[str, Any] | None] = jsonb()
    constraints: Mapped[dict[str, Any] | None] = jsonb()
    parse_source: Mapped[str | None] = mapped_column(Text)
    params_hash: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pending'"))
    error_code: Mapped[str | None] = mapped_column(Text)
    ip_hash: Mapped[str | None] = mapped_column(Text)
    user_agent_hash: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_col()
    completed_at: Mapped[datetime | None] = ts()
    elapsed_ms: Mapped[int | None] = mapped_column(Integer)

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','running','completed','failed','cancelled','degraded')",
            name="ck_trip_requests_status",
        ),
        CheckConstraint(
            "parse_source IS NULL OR parse_source IN ('rule','llm','cache','hybrid')",
            name="ck_trip_requests_parse_source",
        ),
        Index("ix_req_params_hash", "params_hash", text("created_at DESC")),
        Index("ix_req_session", "session_id", text("created_at DESC")),
    )


class Trip(Base):
    __tablename__ = "trips"

    id: Mapped[uuid.UUID] = uuid_pk()
    request_id: Mapped[uuid.UUID] = uuid_fk("trip_requests.id")
    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    city_id: Mapped[uuid.UUID] = uuid_fk("cities.id")
    title: Mapped[str | None] = mapped_column(Text)
    intent_snapshot: Mapped[dict[str, Any]] = jsonb_required()
    days: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    route_count: Mapped[int] = mapped_column(Integer, nullable=False)

    share_slug: Mapped[str | None] = mapped_column(Text, unique=True)
    is_public: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    shared_at: Mapped[datetime | None] = ts()

    generation_ms: Mapped[int | None] = mapped_column(Integer)
    total_cost_cny: Mapped[Decimal] = amount6()
    degraded_modes: Mapped[list[str]] = text_array()
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    parent_trip_id: Mapped[uuid.UUID | None] = uuid_fk("trips.id", ondelete="SET NULL", nullable=True)
    source: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()

    __table_args__ = (
        Index("ix_trips_share", "share_slug", postgresql_where=text("is_public = true")),
        Index("ix_trips_session", "session_id", text("created_at DESC")),
    )


class TripRoute(Base):
    __tablename__ = "trip_routes"

    id: Mapped[uuid.UUID] = uuid_pk()
    trip_id: Mapped[uuid.UUID] = uuid_fk("trips.id")
    label: Mapped[str] = mapped_column(Text, nullable=False)
    archetype: Mapped[str] = mapped_column(Text, nullable=False)
    theme: Mapped[str | None] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    one_liner: Mapped[str | None] = mapped_column(Text)

    total_duration_min: Mapped[int] = mapped_column(Integer, nullable=False)
    total_distance_m: Mapped[int | None] = mapped_column(Integer)
    walking_distance_m: Mapped[int | None] = mapped_column(Integer)
    transit_time_min: Mapped[int | None] = mapped_column(Integer)
    transit_distance_m: Mapped[int | None] = mapped_column(Integer)
    budget_min: Mapped[Decimal | None] = money()
    budget_max: Mapped[Decimal | None] = money()
    budget_scope: Mapped[str | None] = mapped_column(Text, server_default=text("'per_person'"))

    place_count: Mapped[int] = mapped_column(Integer, nullable=False)
    recommend_score: Mapped[float] = mapped_column(Float, nullable=False)
    score_breakdown: Mapped[dict[str, Any]] = jsonb_required()
    best_for: Mapped[list[str]] = text_array()
    highlights: Mapped[list[str]] = text_array()
    pros: Mapped[list[str]] = text_array()
    cons: Mapped[list[str]] = text_array()
    recommendation_reason: Mapped[str | None] = mapped_column(Text)

    feasibility_report: Mapped[dict[str, Any]] = jsonb_required()
    polyline: Mapped[dict[str, Any] | None] = jsonb()
    route_source: Mapped[str | None] = mapped_column(Text)
    template_route_id: Mapped[uuid.UUID | None] = uuid_fk(
        "routes.id", ondelete="SET NULL", nullable=True
    )
    is_selected: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    created_at: Mapped[datetime] = created_at_col()

    __table_args__ = (
        CheckConstraint("label IN ('A','B','C','D')", name="ck_trip_routes_label"),
        CheckConstraint("archetype IN ('relaxed','classic','themed')", name="ck_trip_routes_archetype"),
        CheckConstraint("recommend_score >= 0 AND recommend_score <= 1", name="ck_trip_routes_score"),
        Index("ix_trip_routes_trip", "trip_id", "sort_order"),
    )


class TripRouteStop(Base):
    __tablename__ = "trip_route_stops"

    id: Mapped[uuid.UUID] = uuid_pk()
    trip_route_id: Mapped[uuid.UUID] = uuid_fk("trip_routes.id")
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    # 第几天（1 起）。单日行程恒为 1（server_default 让旧行无需回填）。
    # ``seq`` 仍在**整条方案**里递增，所以前端按 day 分组后不必重新编号。
    day: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    place_id: Mapped[uuid.UUID] = uuid_fk("places.id", ondelete="RESTRICT")
    # 快照：历史行程不随知识库变更而漂移（PRD §8.7 R7）
    place_snapshot: Mapped[dict[str, Any]] = jsonb_required()

    arrive_time: Mapped[time] = mapped_column(Time, nullable=False)
    depart_time: Mapped[time] = mapped_column(Time, nullable=False)
    stay_min: Mapped[int] = mapped_column(Integer, nullable=False)

    transport_mode: Mapped[str | None] = mapped_column(Text)
    transport_min: Mapped[int | None] = mapped_column(Integer)
    transport_distance_m: Mapped[int | None] = mapped_column(Integer)
    transport_source: Mapped[str | None] = mapped_column(Text)

    why_recommended: Mapped[str | None] = mapped_column(Text)
    tips: Mapped[str | None] = mapped_column(Text)
    source_refs: Mapped[dict[str, Any] | None] = jsonb()
    warnings: Mapped[list[str]] = text_array()
    created_at: Mapped[datetime] = created_at_col()

    __table_args__ = (
        UniqueConstraint("trip_route_id", "seq", name="uq_trip_route_stops_seq"),
        CheckConstraint("day >= 1", name="ck_stops_day_positive"),
        CheckConstraint(
            "transport_mode IS NULL OR "
            "transport_mode IN ('walk','metro','bus','taxi','bike','ferry','none')",
            name="ck_stops_transport_mode",
        ),
        CheckConstraint(
            "transport_source IS NULL OR transport_source IN ('amap','osrm','estimated','manual')",
            name="ck_stops_transport_source",
        ),
        Index("ix_stops_route", "trip_route_id", "seq"),
    )


class TripRevision(Base):
    __tablename__ = "trip_revisions"

    id: Mapped[uuid.UUID] = uuid_pk()
    trip_id: Mapped[uuid.UUID] = uuid_fk("trips.id")
    parent_revision_id: Mapped[uuid.UUID | None] = uuid_fk(
        "trip_revisions.id", ondelete="SET NULL", nullable=True
    )
    instruction: Mapped[str | None] = mapped_column(Text)
    parsed_delta: Mapped[dict[str, Any] | None] = jsonb()
    result_trip_id: Mapped[uuid.UUID | None] = uuid_fk("trips.id", ondelete="SET NULL", nullable=True)
    diff_summary: Mapped[dict[str, Any] | None] = jsonb()
    parse_source: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_col()

    __table_args__ = (Index("ix_revisions_trip", "trip_id", text("created_at DESC")),)


class Feedback(Base):
    __tablename__ = "feedback"

    id: Mapped[uuid.UUID] = uuid_pk()
    trip_id: Mapped[uuid.UUID | None] = uuid_fk("trips.id", ondelete="SET NULL", nullable=True)
    place_id: Mapped[uuid.UUID | None] = uuid_fk("places.id", ondelete="SET NULL", nullable=True)
    category: Mapped[str] = mapped_column(Text, nullable=False)
    message: Mapped[str | None] = mapped_column(Text)
    contact: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'open'"))
    created_at: Mapped[datetime] = created_at_col()

    __table_args__ = (
        CheckConstraint(
            "category IN ('wrong_hours','wrong_price','closed','bad_route','wrong_coord','other')",
            name="ck_feedback_category",
        ),
        Index("ix_feedback_created", text("created_at DESC")),
    )


# ════════════════════════════════════════════════════════════════════════════
# 缓存、成本、运维
# ════════════════════════════════════════════════════════════════════════════


class SearchCache(Base):
    __tablename__ = "search_cache"

    id: Mapped[uuid.UUID] = uuid_pk()
    cache_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    query: Mapped[str] = mapped_column(Text, nullable=False)
    locale: Mapped[str | None] = mapped_column(Text, server_default=text("'zh-CN'"))
    result: Mapped[dict[str, Any]] = jsonb_required()
    extracted: Mapped[dict[str, Any] | None] = jsonb()
    cost_cny: Mapped[Decimal] = amount6()
    hit_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    created_at: Mapped[datetime] = created_at_col()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_hit_at: Mapped[datetime | None] = ts()

    __table_args__ = (Index("ix_search_cache_exp", "expires_at"),)


class LlmCache(Base):
    __tablename__ = "llm_cache"

    id: Mapped[uuid.UUID] = uuid_pk()
    cache_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    tier: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_hash: Mapped[str] = mapped_column(Text, nullable=False)
    task: Mapped[str] = mapped_column(Text, nullable=False)
    response: Mapped[dict[str, Any]] = jsonb_required()
    tokens_in: Mapped[int | None] = mapped_column(Integer)
    tokens_out: Mapped[int | None] = mapped_column(Integer)
    tokens_cached: Mapped[int | None] = mapped_column(Integer)
    cost_cny: Mapped[Decimal] = amount6()
    hit_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    created_at: Mapped[datetime] = created_at_col()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_hit_at: Mapped[datetime | None] = ts()

    __table_args__ = (
        CheckConstraint("tier IN ('fast','strong')", name="ck_llm_cache_tier"),
        Index("ix_llm_cache_exp", "expires_at"),
        Index("ix_llm_cache_task", "task"),
    )


class PlanCache(Base):
    __tablename__ = "plan_cache"

    id: Mapped[uuid.UUID] = uuid_pk()
    params_hash: Mapped[str] = mapped_column(Text, nullable=False)
    kb_version: Mapped[str] = mapped_column(Text, nullable=False)
    trip_id: Mapped[uuid.UUID] = uuid_fk("trips.id")
    hit_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    created_at: Mapped[datetime] = created_at_col()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (UniqueConstraint("params_hash", "kb_version", name="uq_plan_cache_key"),)


class CostLog(Base):
    __tablename__ = "cost_logs"

    id: Mapped[int] = bigint_pk()
    request_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    trip_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    category: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str | None] = mapped_column(Text)
    operation: Mapped[str | None] = mapped_column(Text)
    units: Mapped[float] = mapped_column(Float, nullable=False, server_default=text("0"))
    unit_price: Mapped[float] = mapped_column(Float, nullable=False, server_default=text("0"))
    amount_cny: Mapped[Decimal] = amount6()
    cache_hit: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    # 单价是否已从官方价目表校准；未校准时成本数字只是量级参考，报表必须如实标注
    pricing_calibrated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    created_at: Mapped[datetime] = created_at_col()

    __table_args__ = (
        CheckConstraint(
            "category IN ('search','llm','map','geocode','tiles','other')", name="ck_cost_category"
        ),
        Index("ix_cost_created", text("created_at DESC")),
    )


class ApiUsageDaily(Base):
    __tablename__ = "api_usage_daily"

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    provider: Mapped[str] = mapped_column(Text, primary_key=True)
    category: Mapped[str] = mapped_column(Text, primary_key=True)
    calls: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    cache_hits: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    units: Mapped[float] = mapped_column(Float, nullable=False, server_default=text("0"))
    amount_cny: Mapped[Decimal] = amount6()


class RateLimitCounter(Base):
    __tablename__ = "rate_limit_counters"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))


class ErrorLog(Base):
    __tablename__ = "error_logs"

    id: Mapped[int] = bigint_pk()
    request_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    level: Mapped[str] = mapped_column(Text, nullable=False)
    component: Mapped[str] = mapped_column(Text, nullable=False)
    code: Mapped[str | None] = mapped_column(Text)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    context: Mapped[dict[str, Any] | None] = jsonb()
    created_at: Mapped[datetime] = created_at_col()

    __table_args__ = (
        CheckConstraint("level IN ('debug','info','warning','error','critical')", name="ck_error_level"),
        Index("ix_error_created", text("created_at DESC")),
    )


class KbVersion(Base):
    __tablename__ = "kb_versions"

    id: Mapped[uuid.UUID] = uuid_pk()
    version: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    city_id: Mapped[uuid.UUID | None] = uuid_fk("cities.id", ondelete="SET NULL", nullable=True)
    place_count: Mapped[int | None] = mapped_column(Integer)
    route_count: Mapped[int | None] = mapped_column(Integer)
    notes: Mapped[str | None] = mapped_column(Text)
    quality_report: Mapped[dict[str, Any] | None] = jsonb()
    created_at: Mapped[datetime] = created_at_col()
