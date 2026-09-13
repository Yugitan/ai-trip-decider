"""只读目录 API 的集成测试（真实应用 + 真实知识库）。

这组测试守两件事：
1. **端点契约稳定** —— 前端与外部调用方依赖这些字段名与分页语义。
2. **不确定性必须被如实暴露** —— `unknown_fields` / `verification_status` /
   `sources` / `metrics_are_estimated` 一旦被"优化掉"，UI 就再也无法提示用户
   "这条信息可能不准"。这是产品诚实性的最后一道防线，所以用测试钉住。
"""

from __future__ import annotations

import asyncio
import os

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.integration

CITY = "guangzhou"


# ── 城市 ────────────────────────────────────────────────────────────────────


def test_list_cities_reports_real_scale(client: TestClient) -> None:
    body = client.get("/api/v1/cities").json()
    assert body["ok"] is True
    items = body["data"]["items"]
    assert items, "至少应有一个城市"
    guangzhou = next(item for item in items if item["slug"] == CITY)
    assert guangzhou["name"] == "广州"
    assert guangzhou["status"] == "active"
    assert guangzhou["place_count"] >= 200
    assert guangzhou["route_count"] >= 30
    # 内容寻址的版本号（含 10 位摘要），plan_cache 靠它失效
    assert guangzhou["kb_version"] and len(guangzhou["kb_version"].rsplit("-", 1)[-1]) == 10


def test_unknown_city_returns_semantic_error(client: TestClient) -> None:
    response = client.get("/api/v1/cities/shenzhen/places")
    assert response.status_code == 422
    body = response.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "UNSUPPORTED_CITY"
    assert "广州" in body["error"]["hint"]


def test_city_stats_distinguish_known_from_unknown(client: TestClient) -> None:
    data = client.get(f"/api/v1/cities/{CITY}/stats").json()["data"]
    assert data["total_places"] >= 200
    assert data["opening_hours_known"] + data["opening_hours_unknown"] == data["total_places"]
    # 营业时间大部分未知是数据源的客观事实，不能被"优化"成 0
    assert data["opening_hours_unknown"] > 0
    labels = {row["category"]: row["label"] for row in data["categories"]}
    # 类别分布用类别名，不裸露英文键名，也不混入偏好维度的叫法
    assert labels["food"] == "餐饮"
    assert labels["historic"] == "历史人文"
    assert all(row["label"] != row["category"] for row in data["categories"]), "存在未翻译的类别"
    assert all(row["count"] > 0 for row in data["categories"])


# ── 地点 ────────────────────────────────────────────────────────────────────


def test_place_list_is_paginated_and_ordered_by_popularity(client: TestClient) -> None:
    data = client.get(f"/api/v1/cities/{CITY}/places?limit=5").json()["data"]
    assert len(data["items"]) == 5
    assert data["page"]["total"] >= 200
    assert data["page"]["has_more"] is True
    scores = [item["scores"].get("popularity") or 0 for item in data["items"]]
    assert scores == sorted(scores, reverse=True), "列表应按热度降序"


def test_pagination_offsets_do_not_overlap(client: TestClient) -> None:
    first = client.get(f"/api/v1/cities/{CITY}/places?limit=10&offset=0").json()["data"]["items"]
    second = client.get(f"/api/v1/cities/{CITY}/places?limit=10&offset=10").json()["data"]["items"]
    assert {item["id"] for item in first}.isdisjoint({item["id"] for item in second})


def test_page_size_is_capped(client: TestClient) -> None:
    """单页上限 100：不允许一次性把 3776 个地点吐出去。"""
    response = client.get(f"/api/v1/cities/{CITY}/places?limit=1000")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_INPUT"


def test_search_by_alias_works(client: TestClient) -> None:
    """别名搜索是刚需：用户说「小蛮腰」不会说「广州塔」。"""
    data = client.get(f"/api/v1/cities/{CITY}/places?q=小蛮腰").json()["data"]
    assert data["page"]["total"] >= 1
    names = [item["name"] for item in data["items"]]
    assert any("广州塔" in name for name in names), f"别名搜索未命中广州塔：{names}"


def test_search_by_osm_name_via_alias_fallback(client: TestClient) -> None:
    """「太古仓」在 OSM 里叫「太古仓码头」—— 别名必须把两者连起来。"""
    data = client.get(f"/api/v1/cities/{CITY}/places?q=太古仓").json()["data"]
    names = [item["name"] for item in data["items"]]
    assert any("太古仓" in name for name in names), f"别名回退失败：{names}"


def test_category_filter_returns_only_that_category(client: TestClient) -> None:
    data = client.get(f"/api/v1/cities/{CITY}/places?category=cafe&limit=10").json()["data"]
    assert data["items"]
    assert all(item["category"] == "cafe" for item in data["items"])


def test_unknown_category_is_rejected_with_hint(client: TestClient) -> None:
    response = client.get(f"/api/v1/cities/{CITY}/places?category=karaoke")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_INPUT"
    assert "合法类别" in response.json()["error"]["hint"]


def test_place_payload_exposes_uncertainty(client: TestClient) -> None:
    """★ 核心断言：不确定性必须原样暴露，不能被服务端"抹平"。"""
    data = client.get(f"/api/v1/cities/{CITY}/places?limit=20").json()["data"]
    for item in data["items"]:
        assert item["verification_status"] in ("verified", "probable", "unknown", "stale", "conflicting")
        assert item["score_source"] in ("curated", "derived")
        assert isinstance(item["unknown_fields"], list)
        assert isinstance(item["data_quality_flags"], list)
        assert item["sources"], f"{item['name']} 缺少来源信息"
        source = item["sources"][0]
        # 来源必须能说明"它支持哪些字段"，否则等于一句空话
        assert source["field_scope"], f"{item['name']} 的来源没有 field_scope"
        assert source["url"], f"{item['name']} 的来源没有 URL"


def test_place_scores_are_within_range(client: TestClient) -> None:
    data = client.get(f"/api/v1/cities/{CITY}/places?limit=50").json()["data"]
    for item in data["items"]:
        for dim, value in item["scores"].items():
            assert 0.0 <= value <= 1.0, f"{item['name']} 的 {dim}={value} 越界"


def test_place_detail_includes_traceability_fields(client: TestClient) -> None:
    listed = client.get(f"/api/v1/cities/{CITY}/places?q=广州塔").json()["data"]["items"]
    assert listed
    place_id = listed[0]["id"]
    detail = client.get(f"/api/v1/places/{place_id}").json()["data"]
    assert detail["id"] == place_id
    assert detail["external_source"] == "OpenStreetMap"
    assert detail["external_id"], "缺少外部 ID，无法回溯到原始数据"
    assert detail["source_url"].startswith("https://www.openstreetmap.org/")
    assert detail["canonical_name"]


def test_missing_place_returns_semantic_error(client: TestClient) -> None:
    response = client.get("/api/v1/places/00000000-0000-0000-0000-000000000000")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "PLACE_NOT_FOUND"


def test_malformed_place_id_is_rejected(client: TestClient) -> None:
    response = client.get("/api/v1/places/not-a-uuid")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_INPUT"


# ── 路线 ────────────────────────────────────────────────────────────────────


def test_routes_include_ordered_stops(client: TestClient) -> None:
    data = client.get(f"/api/v1/cities/{CITY}/routes").json()["data"]
    assert data["total"] >= 30
    route = data["items"][0]
    assert route["stops"], "路线必须带站点明细"
    assert [stop["seq"] for stop in route["stops"]] == list(range(len(route["stops"])))
    for stop in route["stops"]:
        assert stop["place_name"] and stop["category"]
        assert stop["stay_min"] and stop["stay_min"] > 0


def test_route_metrics_are_flagged_as_estimated(client: TestClient) -> None:
    """路线时长与步行距离是**估算值**，接口必须明说，UI 才能如实标注。"""
    data = client.get(f"/api/v1/cities/{CITY}/routes").json()["data"]
    for route in data["items"]:
        assert route["metrics_are_estimated"] is True
        assert route["duration_min"] is None or route["duration_min"] > 0
        assert route["walking_distance_m"] is None or route["walking_distance_m"] >= 0


def test_route_budget_is_empty_rather_than_fabricated(client: TestClient) -> None:
    """★ 没有价格来源时预算必须留空，而不是填一个看起来合理的数字。"""
    data = client.get(f"/api/v1/cities/{CITY}/routes").json()["data"]
    assert all(route["estimated_budget"] is None for route in data["items"])


def test_routes_can_be_filtered_by_archetype(client: TestClient) -> None:
    for archetype in ("relaxed", "classic", "themed"):
        data = client.get(f"/api/v1/cities/{CITY}/routes?archetype={archetype}").json()["data"]
        assert data["total"] >= 5, f"{archetype} 只有 {data['total']} 条模板"
        assert all(route["archetype_hint"] == archetype for route in data["items"])


def test_route_type_filter_works(client: TestClient) -> None:
    data = client.get(f"/api/v1/cities/{CITY}/routes?route_type=food").json()["data"]
    assert data["total"] >= 1
    assert all(route["route_type"] == "food" for route in data["items"])


def test_unknown_archetype_is_rejected(client: TestClient) -> None:
    response = client.get(f"/api/v1/cities/{CITY}/routes?archetype=extreme")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_INPUT"


def test_routes_without_stops_are_lighter(client: TestClient) -> None:
    with_stops = client.get(f"/api/v1/cities/{CITY}/routes?with_stops=true").json()["data"]["items"][0]
    without = client.get(f"/api/v1/cities/{CITY}/routes?with_stops=false").json()["data"]["items"][0]
    assert with_stops["stops"] and not without["stops"]


# ── 元信息 ──────────────────────────────────────────────────────────────────


def test_scoring_config_is_public_and_consistent(client: TestClient) -> None:
    """排序规则不应是黑箱：权重与偏好维度对外可查，且权重之和为 1。"""
    data = client.get("/api/v1/meta/scoring-config").json()["data"]
    assert abs(sum(data["default_weights"].values()) - 1.0) < 1e-9
    for key, archetype in data["archetypes"].items():
        assert abs(sum(archetype["weights"].values()) - 1.0) < 1e-9, f"{key} 权重和不为 1"
    dimensions = {dim["key"] for dim in data["preference_dimensions"]}
    assert {"food", "photo", "culture", "night_view", "family", "couple"} <= dimensions
    for dim in data["preference_dimensions"]:
        assert dim["target"], f"{dim['key']} 没有说明它作用在哪个分值字段或类别上"


# ── 查询次数：N+1 回归 ──────────────────────────────────────────────────────
#
# 为什么需要这组测试：`/cities` 的响应体在 N+1 和批量聚合两种实现下**完全一样**，
# 所以任何基于响应内容的断言都抓不到回归。只有数一数实际发了多少条 SQL 才管用。
# 当前只有广州一个城市时 N+1 也看不出来 —— 必须真的加一个城市进去比。

TEMP_CITY_SLUG = "zz-query-count-probe"


async def _set_temp_city(present: bool) -> None:
    """在测试库里插入/删除一个临时城市（无地点、无路线，不污染知识库断言）。"""
    from sqlalchemy import delete
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.db.models import City

    engine = create_async_engine(os.environ["DATABASE_URL"])
    try:
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as session:
            await session.execute(delete(City).where(City.slug == TEMP_CITY_SLUG))
            if present:
                session.add(City(slug=TEMP_CITY_SLUG, name="查询计数探针", status="active"))
            await session.commit()
    finally:
        await engine.dispose()


def _count_queries_for_cities(client: TestClient) -> int:
    """发一次 /cities，返回它实际执行的 SQL 条数（不含连接池的 pre-ping 探针）。"""
    from sqlalchemy import event

    from app.db.session import get_engine

    statements: list[str] = []

    def _record(conn: object, cursor: object, statement: str, *args: object) -> None:
        # pool_pre_ping=True 会在每次取连接时发一条 SELECT 1；它与业务查询无关，
        # 计入会让计数随连接池状态波动，因此排除掉。
        if statement.strip().upper().startswith("SELECT 1"):
            return
        statements.append(statement)

    engine = get_engine()
    event.listen(engine.sync_engine, "before_cursor_execute", _record)
    try:
        assert client.get("/api/v1/cities").status_code == 200
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _record)
    return len(statements)


def test_list_cities_query_count_does_not_grow_with_city_count(client: TestClient) -> None:
    """★ 回归：`/cities` 曾经是 1 + 3N 的 N+1 —— 每个城市各查 3 次（地点数/路线数/最新版本）。

    断言的是「查询次数不随城市数增长」，而不是某个具体数字：
    这样以后即使合理地增删一条查询，这条测试也不会误报；
    但只要有人再写出按城市循环的查询，它立刻会失败。
    """
    asyncio.run(_set_temp_city(False))
    try:
        baseline = _count_queries_for_cities(client)

        asyncio.run(_set_temp_city(True))
        with_extra_city = _count_queries_for_cities(client)
    finally:
        asyncio.run(_set_temp_city(False))

    assert with_extra_city == baseline, (
        f"多了一个城市后查询次数从 {baseline} 涨到 {with_extra_city} —— "
        "说明 `/cities` 又变成了按城市循环的 N+1"
    )
    assert baseline <= 5, f"单次 /cities 发了 {baseline} 条查询，超出批量聚合的预期"


def test_temp_city_appears_with_zero_counts(client: TestClient) -> None:
    """临时城市没有地点与路线，计数必须是 0 而不是缺字段 —— 顺带验证聚合的默认值。"""
    asyncio.run(_set_temp_city(True))
    try:
        items = client.get("/api/v1/cities").json()["data"]["items"]
    finally:
        asyncio.run(_set_temp_city(False))

    probe = next((item for item in items if item["slug"] == TEMP_CITY_SLUG), None)
    assert probe is not None, "临时城市没有被 /cities 返回"
    assert probe["place_count"] == 0
    assert probe["route_count"] == 0
    assert probe["kb_version"] is None


def test_list_cities_keeps_guangzhou_after_batching(client: TestClient) -> None:
    """批量化后广州的统计值必须与单城查询时一致（防止聚合键接错）。"""
    items = client.get("/api/v1/cities").json()["data"]["items"]
    guangzhou = next(item for item in items if item["slug"] == CITY)
    assert guangzhou["place_count"] >= 200
    assert guangzhou["route_count"] >= 30
    assert guangzhou["kb_version"], "批量取最新版本失败，kb_version 不该为空"
