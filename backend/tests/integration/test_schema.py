"""数据库 schema 与完整性规则的集成测试。

把 PRD §8.7 的「数据完整性硬规则」变成**可执行断言**，而不是文档里的口号。
这些约束由数据库层强制，因此即使有人绕过 ORM 直接写 SQL 也会被拒绝。
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

EXPECTED_TABLES = {
    "cities",
    "places",
    "place_aliases",
    "place_external_ids",
    "travel_sources",
    "place_sources",
    "routes",
    "route_places",
    "place_relations",
    "merge_logs",
    "trip_requests",
    "trips",
    "trip_routes",
    "trip_route_stops",
    "trip_revisions",
    "feedback",
    "search_cache",
    "llm_cache",
    "plan_cache",
    "cost_logs",
    "api_usage_daily",
    "rate_limit_counters",
    "error_logs",
    "kb_versions",
}


async def _tables(session: AsyncSession) -> set[str]:
    rows = await session.execute(
        text("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")
    )
    return {r[0] for r in rows}


async def test_all_expected_tables_exist(db_session: AsyncSession) -> None:
    missing = EXPECTED_TABLES - await _tables(db_session)
    assert not missing, f"缺少数据表：{sorted(missing)}"


async def test_pg_trgm_extension_is_installed(db_session: AsyncSession) -> None:
    """pg_trgm 是中文模糊匹配（实体去重）的前置依赖，缺失会导致去重降级。"""
    found = (await db_session.execute(text("SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm'"))).scalar()
    assert found == 1


async def test_name_trgm_index_uses_gin_trgm_ops(db_session: AsyncSession) -> None:
    ddl = (
        await db_session.execute(
            text("SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_places_name_trgm'")
        )
    ).scalar()
    assert ddl is not None, "ix_places_name_trgm 不存在"
    assert "gin_trgm_ops" in ddl
    assert "USING gin" in ddl


async def test_canonical_name_index_is_not_unique(db_session: AsyncSession) -> None:
    """规范名索引**必须不是唯一索引**。

    早期版本把它设成 UNIQUE，被真实数据立刻打脸：广州有 9 个"中山公园"、
    9 个"图书馆"、不止一处"广东美术馆" —— 它们是**不同的地点**。
    实体身份的正确判据是「同名 + 地理邻近」（地理围栏），不是"名称全局唯一"。
    这条测试守住这个结论，防止有人"顺手加个 unique"把真实同名地点挡在库外。
    """
    ddl = (
        await db_session.execute(
            text("SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_places_city_canonical'")
        )
    ).scalar()
    assert ddl is not None, "ix_places_city_canonical 不存在（查找性能依赖它）"
    assert "UNIQUE" not in ddl, f"规范名索引不应唯一：{ddl}"
    assert "lower(" in ddl


async def test_same_name_places_can_coexist(db_session: AsyncSession) -> None:
    """同名但不同位置的地点必须能同时存在（回归：曾因唯一约束导致建库失败）。"""
    dup_names = (
        await db_session.execute(
            text(
                """
                SELECT lower(canonical_name) AS n, count(*) AS c FROM places
                GROUP BY lower(canonical_name) HAVING count(*) > 1
                ORDER BY c DESC LIMIT 3
                """
            )
        )
    ).all()
    assert dup_names, "知识库里应当存在同名不同地点（如中山公园）；若为空说明去重过度"
    for _name, count in dup_names:
        assert count >= 2


async def test_alias_uniqueness_is_scoped_to_place(db_session: AsyncSession) -> None:
    """别名的唯一性只能约束在"单个地点内部"，跨地点允许同名别名。"""
    place_id = (
        await db_session.execute(text("SELECT place_id FROM place_aliases LIMIT 1"))
    ).scalar()
    if place_id is None:
        pytest.skip("没有别名数据")
    with pytest.raises(IntegrityError, match="uq_alias_place_norm"):
        await db_session.execute(
            text(
                "INSERT INTO place_aliases (place_id, city_id, alias, alias_norm, alias_type)"
                " SELECT place_id, city_id, alias, alias_norm, alias_type FROM place_aliases LIMIT 1"
            )
        )
    await db_session.rollback()


# ── 规则 R1–R8：用真实 INSERT 验证约束确实生效 ──────────────────────────────


async def _make_city(session: AsyncSession, slug: str) -> uuid.UUID:
    await session.execute(text("DELETE FROM cities WHERE slug = :s"), {"s": slug})
    row = await session.execute(
        text(
            "INSERT INTO cities (slug, name, status) VALUES (:s, :n, 'active') RETURNING id"
        ),
        {"s": slug, "n": f"测试城市-{slug}"},
    )
    city_id: uuid.UUID = row.scalar_one()
    return city_id


async def _insert_place(session: AsyncSession, **overrides: object) -> None:
    payload: dict[str, object] = {
        "city_id": overrides["city_id"],
        "canonical_name": "测试地点",
        "display_name": "测试地点",
        "category": "attraction",
        "latitude": 23.1291,
        "longitude": 113.2644,
    }
    payload.update({k: v for k, v in overrides.items() if k != "city_id"})
    columns = ", ".join(payload)
    values = ", ".join(f":{k}" for k in payload)
    await session.execute(text(f"INSERT INTO places ({columns}) VALUES ({values})"), payload)


async def test_r1_coordinates_cannot_be_zero(db_session: AsyncSession) -> None:
    city = await _make_city(db_session, "test-r1")
    with pytest.raises(IntegrityError, match="ck_places_coord_not_zero"):
        await _insert_place(db_session, city_id=city, latitude=0, longitude=0)
    await db_session.rollback()


async def test_r3_verified_requires_source_url(db_session: AsyncSession) -> None:
    """规则 R3：声明 verified 就必须有 source_url —— 从根上阻断"伪造来源"。"""
    city = await _make_city(db_session, "test-r3")
    with pytest.raises(IntegrityError, match="ck_places_verified_needs_source"):
        await _insert_place(
            db_session,
            city_id=city,
            canonical_name="无来源却声称已核实",
            display_name="无来源却声称已核实",
            verification_status="verified",
            source_url=None,
        )
    await db_session.rollback()


async def test_category_enum_is_enforced(db_session: AsyncSession) -> None:
    city = await _make_city(db_session, "test-cat")
    with pytest.raises(IntegrityError, match="ck_places_category"):
        await _insert_place(db_session, city_id=city, category="not_a_real_category")
    await db_session.rollback()


async def test_price_order_is_enforced(db_session: AsyncSession) -> None:
    city = await _make_city(db_session, "test-price")
    with pytest.raises(IntegrityError, match="ck_places_price_order"):
        await _insert_place(db_session, city_id=city, price_min=100, price_max=50)
    await db_session.rollback()


async def test_duration_range_is_enforced(db_session: AsyncSession) -> None:
    city = await _make_city(db_session, "test-dur")
    with pytest.raises(IntegrityError, match="ck_places_duration_range"):
        await _insert_place(db_session, city_id=city, recommended_duration_min=5)
    await db_session.rollback()


async def test_relation_pair_order_is_enforced(db_session: AsyncSession) -> None:
    """规则 R6：place_relations 只存无序对，防止 (a,b) 与 (b,a) 重复存储。"""
    city = await _make_city(db_session, "test-rel")
    high, low = uuid.uuid4(), uuid.uuid4()
    a, b = max(high, low), min(high, low)
    # 先造两个地点（外键要求）
    for name, pid in (("rel-a", a), ("rel-b", b)):
        await db_session.execute(
            text(
                "INSERT INTO places (id, city_id, canonical_name, display_name, category,"
                " latitude, longitude) VALUES (:id, :city, :n, :n, 'attraction', 23.1, 113.2)"
            ),
            {"id": pid, "city": city, "n": name},
        )
    with pytest.raises(IntegrityError, match="ck_relation_order"):
        await db_session.execute(
            text(
                "INSERT INTO place_relations (city_id, place_a_id, place_b_id, data_source)"
                " VALUES (:city, :a, :b, 'estimated')"
            ),
            {"city": city, "a": a, "b": b},
        )
    await db_session.rollback()


async def test_relation_unique_pair(db_session: AsyncSession) -> None:
    city = await _make_city(db_session, "test-rel2")
    a, b = sorted([uuid.uuid4(), uuid.uuid4()])
    for name, pid in (("rel2-a", a), ("rel2-b", b)):
        await db_session.execute(
            text(
                "INSERT INTO places (id, city_id, canonical_name, display_name, category,"
                " latitude, longitude) VALUES (:id, :city, :n, :n, 'attraction', 23.1, 113.2)"
            ),
            {"id": pid, "city": city, "n": name},
        )
    insert = text(
        "INSERT INTO place_relations (city_id, place_a_id, place_b_id, data_source, distance_m)"
        " VALUES (:city, :a, :b, 'estimated', 1200)"
    )
    await db_session.execute(insert, {"city": city, "a": a, "b": b})
    with pytest.raises(IntegrityError, match="uq_relation_pair"):
        await db_session.execute(insert, {"city": city, "a": a, "b": b})
    await db_session.rollback()


async def test_r7_trip_stop_requires_place_snapshot(db_session: AsyncSession) -> None:
    """规则 R7：行程站点必须带地点快照，否则知识库变更会让历史行程漂移。"""
    nullable = (
        await db_session.execute(
            text(
                "SELECT is_nullable FROM information_schema.columns"
                " WHERE table_name = 'trip_route_stops' AND column_name = 'place_snapshot'"
            )
        )
    ).scalar()
    assert nullable == "NO"


async def test_plan_cache_key_is_unique_on_hash_and_kb_version(db_session: AsyncSession) -> None:
    ddl = (
        await db_session.execute(
            text("SELECT indexdef FROM pg_indexes WHERE indexname = 'uq_plan_cache_key'")
        )
    ).scalar()
    assert ddl is not None and "UNIQUE" in ddl


async def test_cost_logs_category_is_constrained(db_session: AsyncSession) -> None:
    with pytest.raises(IntegrityError, match="ck_cost_category"):
        await db_session.execute(
            text(
                "INSERT INTO cost_logs (category, provider, amount_cny) VALUES ('bogus', 'x', 0.1)"
            )
        )
    await db_session.rollback()
