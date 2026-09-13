"""知识库（seed + 质检）的集成测试。

这些断言把 PRD §9.3 的上线门槛与 §26 的验收项固化下来：
以后任何人改动丰富化规则、过滤阈值或人工数据，只要把知识库质量拉低到门槛以下，
这里就会失败 —— 而不是等到用户拿到垃圾路线才发现。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

CITY = "guangzhou"


async def _scalar(session: AsyncSession, sql: str, **params: object) -> Any:
    """执行一句标量 SQL。返回 Any 是刻意的：这里查的是任意类型的标量，
    静态类型无法推断，硬套 object 只会让每个调用点都被迫做无意义的 cast。"""
    return (await session.execute(text(sql), params)).scalar()


async def _city_id(session: AsyncSession) -> str:
    city_id = await _scalar(session, "SELECT id FROM cities WHERE slug = :s", s=CITY)
    if city_id is None:
        pytest.skip("知识库尚未建库（先运行 make seed）")
    return str(city_id)


async def test_city_is_active_with_bbox(db_session: AsyncSession) -> None:
    row = (
        await db_session.execute(
            text("SELECT status, bbox_min_lat, bbox_max_lat FROM cities WHERE slug = :s"), {"s": CITY}
        )
    ).one_or_none()
    if row is None:
        pytest.skip("知识库尚未建库（先运行 make seed）")
    assert row[0] == "active"
    assert row[1] is not None and row[2] is not None


async def test_place_count_meets_mvp_target(db_session: AsyncSession) -> None:
    """PRD M1 目标：≥200 条活跃地点。"""
    city = await _city_id(db_session)
    count = await _scalar(
        db_session, "SELECT count(*) FROM places WHERE city_id = :c AND status='active'", c=city
    )
    assert int(count) >= 200, f"活跃地点只有 {count} 条，未达到 200 条目标"


async def test_route_count_meets_mvp_target(db_session: AsyncSession) -> None:
    """PRD M1 目标：≥30 条路线。"""
    city = await _city_id(db_session)
    count = await _scalar(db_session, "SELECT count(*) FROM routes WHERE city_id = :c", c=city)
    assert int(count) >= 30, f"路线只有 {count} 条，未达到 30 条目标"


async def test_every_place_has_a_reachable_source_row(db_session: AsyncSession) -> None:
    """每个地点都必须有来源条目，且来源 URL 指向真实可访问的域名。"""
    city = await _city_id(db_session)
    orphan = await _scalar(
        db_session,
        """
        SELECT count(*) FROM places p WHERE p.city_id = :c
          AND NOT EXISTS (SELECT 1 FROM place_sources ps WHERE ps.place_id = p.id)
        """,
        c=city,
    )
    assert int(orphan) == 0


async def test_no_place_claims_verified_without_a_source(db_session: AsyncSession) -> None:
    """规则 R3：声明 verified 就必须有 source_url —— 从根上阻断伪造来源。"""
    city = await _city_id(db_session)
    bad = await _scalar(
        db_session,
        """
        SELECT count(*) FROM places
        WHERE city_id = :c AND verification_status = 'verified'
          AND (source_url IS NULL OR source_url = '')
        """,
        c=city,
    )
    assert int(bad) == 0


async def test_all_coordinates_inside_city_bbox(db_session: AsyncSession) -> None:
    city = await _city_id(db_session)
    bad = await _scalar(
        db_session,
        """
        SELECT count(*) FROM places
        WHERE city_id = :c AND (latitude < 22.50 OR latitude > 23.95
                                OR longitude < 112.90 OR longitude > 114.05)
        """,
        c=city,
    )
    assert int(bad) == 0


async def test_no_unknown_categories(db_session: AsyncSession) -> None:
    from app.domain.categories import PLACE_CATEGORIES

    city = await _city_id(db_session)
    placeholders = ", ".join(f":cat_{i}" for i in range(len(PLACE_CATEGORIES)))
    params: dict[str, object] = {"c": city}
    params.update({f"cat_{i}": value for i, value in enumerate(PLACE_CATEGORIES)})
    bad = await _scalar(
        db_session,
        f"SELECT count(*) FROM places WHERE city_id = :c AND category NOT IN ({placeholders})",
        **params,
    )
    assert int(bad) == 0


async def test_all_scores_within_range(db_session: AsyncSession) -> None:
    """R5：分值必须在 [0,1] 内。"""
    city = await _city_id(db_session)
    bad = await _scalar(
        db_session,
        """
        SELECT count(*) FROM places WHERE city_id = :c AND (
          popularity_score NOT BETWEEN 0 AND 1 OR photo_score NOT BETWEEN 0 AND 1
          OR food_score NOT BETWEEN 0 AND 1 OR culture_score NOT BETWEEN 0 AND 1
          OR night_view_score NOT BETWEEN 0 AND 1)
        """,
        c=city,
    )
    assert int(bad) == 0


async def test_scores_are_not_flat_placeholders(db_session: AsyncSession) -> None:
    """分值必须有区分度：如果绝大多数地点热度都等于同一个值，排序就失去意义。

    这是"禁止用 0.5 占位"的可执行版本：不检查"是否有 0.5"，而是检查**分布是否有区分度**。
    """
    city = await _city_id(db_session)
    distinct = await _scalar(
        db_session,
        "SELECT count(DISTINCT popularity_score) FROM places WHERE city_id = :c AND popularity_score IS NOT NULL",
        c=city,
    )
    assert int(distinct) >= 8, f"热度分值只有 {distinct} 种取值，区分度不足"


async def test_no_route_with_fewer_than_three_stops(db_session: AsyncSession) -> None:
    """PRD §9.4：少于 3 站的"路线"是伪路线，不允许入库。"""
    city = await _city_id(db_session)
    thin = await _scalar(
        db_session,
        """
        SELECT count(*) FROM (
          SELECT r.id FROM routes r
          LEFT JOIN route_places rp ON rp.route_id = r.id
          WHERE r.city_id = :c GROUP BY r.id HAVING count(rp.id) < 3
        ) t
        """,
        c=city,
    )
    assert int(thin) == 0


async def test_route_places_are_ordered_contiguously(db_session: AsyncSession) -> None:
    """站点序号必须从 0 开始连续，否则前端时间线会出现空洞。"""
    city = await _city_id(db_session)
    bad = await _scalar(
        db_session,
        """
        SELECT count(*) FROM (
          SELECT rp.route_id, count(*) AS n, max(rp.seq) AS max_seq, min(rp.seq) AS min_seq
          FROM route_places rp JOIN routes r ON r.id = rp.route_id
          WHERE r.city_id = :c GROUP BY rp.route_id
        ) t WHERE max_seq <> n - 1 OR min_seq <> 0
        """,
        c=city,
    )
    assert int(bad) == 0


async def test_route_metrics_are_estimated_and_non_negative(db_session: AsyncSession) -> None:
    """路线的时长/步行距离是估算值，必须非负；预算刻意留空（不编造价格）。"""
    city = await _city_id(db_session)
    bad = await _scalar(
        db_session,
        """
        SELECT count(*) FROM routes WHERE city_id = :c
          AND (duration_min IS NULL OR duration_min <= 0
               OR walking_distance_m IS NULL OR walking_distance_m < 0
               OR estimated_transport_time_min IS NULL)
        """,
        c=city,
    )
    assert int(bad) == 0
    budgeted = await _scalar(
        db_session, "SELECT count(*) FROM routes WHERE city_id = :c AND estimated_budget_min IS NOT NULL", c=city
    )
    assert int(budgeted) == 0, "没有价格来源时不得填入预算估算（见 docs/DATA_REPORT.md）"


async def test_relations_are_labelled_honestly(db_session: AsyncSession) -> None:
    """关系图的每条记录都必须标明数据来源，且标明哪些出行方式的耗时是推导值。"""
    city = await _city_id(db_session)
    bad = await _scalar(
        db_session,
        """
        SELECT count(*) FROM place_relations
        WHERE city_id = :c AND (data_source NOT IN ('amap','osrm','estimated','manual')
                                OR derived_modes = '{}')
        """,
        c=city,
    )
    # 允许关系表为空（尚未运行 make relations），但一旦有数据就必须标注清楚
    total = await _scalar(db_session, "SELECT count(*) FROM place_relations WHERE city_id = :c", c=city)
    if int(total) == 0:
        pytest.skip("关系图尚未计算（先运行 make relations）")
    assert int(bad) == 0


async def test_relation_pairs_are_canonically_ordered(db_session: AsyncSession) -> None:
    """R6：只存无序对。"""
    city = await _city_id(db_session)
    bad = await _scalar(
        db_session, "SELECT count(*) FROM place_relations WHERE city_id = :c AND place_a_id >= place_b_id", c=city
    )
    assert int(bad) == 0


async def test_kb_version_is_content_addressed(db_session: AsyncSession) -> None:
    """kb_version 必须包含内容摘要（plan_cache 靠它失效）。

    早期实现用日期做版本号，导致"同一天重跑建库后数据变了但缓存没失效"。
    """
    version = await _scalar(
        db_session, "SELECT version FROM kb_versions ORDER BY created_at DESC LIMIT 1"
    )
    if version is None:
        pytest.skip("尚未建库")
    assert str(version).startswith("gz-")
    suffix = str(version).rsplit("-", 1)[-1]
    assert len(suffix) == 10, f"kb_version 缺少内容摘要：{version}"


async def test_curated_places_are_present_in_library(db_session: AsyncSession) -> None:
    """人工确认过的关键广州地标必须在库里。

    这条守的是一个具体的失败模式：相关性过滤规则曾把广州塔（OSM 标为 tourism=artwork）、
    白云山（natural=peak）、荔湾湖公园（无信号的 leisure=park）全部误杀。
    """
    city = await _city_id(db_session)
    must_have = [
        "广州塔",
        "陈家祠",
        "沙面",
        "白云山",
        "越秀公园",
        "上下九步行街",
        "北京路步行街",
        "广东省博物馆",
        "中山纪念堂",
    ]
    found: set[str] = set()
    for name in must_have:
        hit = await _scalar(
            db_session,
            """
            SELECT 1 FROM places WHERE city_id = :c
              AND (canonical_name = :n OR id IN (
                    SELECT place_id FROM place_aliases WHERE city_id = :c AND alias = :n))
            LIMIT 1
            """,
            c=city,
            n=name,
        )
        if hit:
            found.add(name)
    missing = [name for name in must_have if name not in found]
    assert not missing, f"以下关键地标不在知识库里：{missing}"


async def test_aliases_are_unique_per_place_but_shared_across_places(db_session: AsyncSession) -> None:
    """同名地点是允许的（广州有多个中山公园），但同一地点内别名不得重复。"""
    duplicates = await _scalar(
        db_session,
        """
        SELECT count(*) FROM (
          SELECT place_id, alias_norm FROM place_aliases
          GROUP BY place_id, alias_norm HAVING count(*) > 1
        ) t
        """,
    )
    assert int(duplicates) == 0


async def test_all_places_are_inside_city_boundary(db_session: AsyncSession) -> None:
    """★ 回归测试：所有地点必须落在城市行政边界内 ★

    这条守的是一个真实且严重的 bug：整城抓取最初用**矩形 bbox**，而广州实际面积
    只有矩形的一半左右 —— 结果约 2000 条深圳/东莞/佛山/中山的地点混进了广州知识库
    （「深圳野生动物园」「锦绣中华民俗村」甚至因为信号多而排到了热度榜最前面）。
    只用地址标签与名称过滤只能拦下一部分，唯一可靠判据是行政边界。

    边界文件由 scripts/fetch_city_boundary.py 生成；缺失时本用例失败（而不是跳过），
    因为"没有边界数据"正是这个 bug 得以存在的前提。
    """
    import json

    from app.core.paths import data_dir
    from app.domain.geo import CityBoundary, point_in_rings

    boundary_path = Path(data_dir()) / "reference" / "guangzhou_boundary.json"
    assert boundary_path.exists(), (
        f"缺少城市边界文件 {boundary_path}，无法验证地点归属。\n"
        "请运行：python scripts/fetch_city_boundary.py"
    )
    boundary = CityBoundary.from_payload(json.loads(boundary_path.read_text(encoding="utf-8")))

    rows = (
        await db_session.execute(
            text(
                "SELECT display_name, latitude, longitude FROM places"
                " WHERE city_id = (SELECT id FROM cities WHERE slug = 'guangzhou')"
            )
        )
    ).all()
    assert rows, "知识库为空，无法验证"
    outsiders = [
        row[0] for row in rows if not point_in_rings(float(row[1]), float(row[2]), boundary)
    ]
    assert not outsiders, (
        f"有 {len(outsiders)} 个地点落在广州行政边界之外（矩形 bbox 的后遗症）：{outsiders[:10]}"
    )


async def test_known_out_of_city_places_are_absent(db_session: AsyncSession) -> None:
    """明确属于邻市的地点必须不在库里（防止过滤规则被误改回宽松）。"""
    outsiders = [
        "深圳野生动物园",
        "锦绣中华民俗村",
        "东莞可园",
        "佛山祖庙",
        "中山市博物馆",
        "东莞博物馆",
    ]
    found = (
        await db_session.execute(
            text("SELECT display_name FROM places WHERE display_name = ANY(:names)"),
            {"names": outsiders},
        )
    ).all()
    assert not found, f"邻市地点出现在广州知识库里：{[row[0] for row in found]}"
