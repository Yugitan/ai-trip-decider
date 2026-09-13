"""人工整理数据文件（data/curated/*.yaml）的单元测试。

为什么需要这组测试：
    人工数据是**最容易写错的一环** —— 实测过程中就出现过 scores 里写了非法维度
    （`shopping:` / `quiet:`）、12 条路线只有 2 个站点、2 条 `route_type` 写错、
    路线引用了不存在的地点名。这些错误如果等到建库时才暴露，代价是"改 YAML → 跑建库
    → 发现错误 → 再改"的长循环。

    这组测试**不需要数据库、不需要网络**，直接对真实数据文件做校验，
    因此改完 YAML 可以秒级得到反馈。

注意：这里断言的是"数据本身的合法性与自洽性"，不是"知识库最终质量"。
后者由 `tests/integration/test_knowledge_base.py` 与 `scripts/validate_seed.py` 负责。
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

from app.core.config import SCORE_DIMENSIONS, get_seed_config
from app.core.paths import curated_data_dir
from app.domain.categories import PLACE_CATEGORIES
from app.schemas.curated import (
    CuratedPlaceFile,
    CuratedRouteFile,
    load_curated_places,
    load_curated_routes,
)

pytestmark = pytest.mark.unit

PLACES_FILE = "guangzhou_places.yaml"
ROUTES_FILE = "guangzhou_routes.yaml"


@pytest.fixture(scope="module")
def places() -> CuratedPlaceFile:
    return load_curated_places(curated_data_dir() / PLACES_FILE)


@pytest.fixture(scope="module")
def routes() -> CuratedRouteFile:
    return load_curated_routes(curated_data_dir() / ROUTES_FILE)


# ── 文件存在与 Schema 校验 ─────────────────────────────────────────────────


@pytest.mark.parametrize("filename", [PLACES_FILE, ROUTES_FILE])
def test_curated_file_exists(filename: str) -> None:
    path = Path(curated_data_dir()) / filename
    assert path.exists(), f"缺少人工数据文件：{path}"
    assert path.stat().st_size > 1000, f"{filename} 内容过少，疑似被误清空"


def test_places_file_passes_schema(places: CuratedPlaceFile) -> None:
    """能加载即通过 Schema（非法类别/非法分值维度/分值越界都会在这里失败）。"""
    assert places.city == "guangzhou"
    assert len(places.places) >= 40, f"人工地点只有 {len(places.places)} 条，覆盖不足"


def test_routes_file_passes_schema(routes: CuratedRouteFile) -> None:
    """能加载即通过 Schema（<3 站、slug 重复、非法 route_type 都会在这里失败）。"""
    assert routes.city == "guangzhou"
    assert len(routes.routes) >= 30, f"路线模板只有 {len(routes.routes)} 条，未达到 30 条目标"


# ── 分值维度：与 config/seed.yaml 的推导规则保持同一套维度 ──────────────────


def test_curated_scores_only_use_known_dimensions(places: CuratedPlaceFile) -> None:
    offenders = {
        place.name: sorted(set(place.scores) - set(SCORE_DIMENSIONS))
        for place in places.places
        if set(place.scores) - set(SCORE_DIMENSIONS)
    }
    assert not offenders, f"人工数据使用了未知分值维度：{offenders}"


def test_curated_scores_are_within_range(places: CuratedPlaceFile) -> None:
    for place in places.places:
        for dim, value in place.scores.items():
            assert 0.0 <= value <= 1.0, f"{place.name} 的 {dim}={value} 越界"


def test_curated_categories_are_valid(places: CuratedPlaceFile) -> None:
    bad = [
        (place.name, place.category)
        for place in places.places
        if place.category is not None and place.category not in PLACE_CATEGORIES
    ]
    assert not bad, f"人工数据使用了非法类别：{bad}"


# ── 覆盖度：人工数据要真的能"扶起"被规则误杀的类别 ─────────────────────────


def test_curated_covers_key_preference_dimensions(places: CuratedPlaceFile) -> None:
    """人工地点必须覆盖主要偏好维度，否则冷启动时排序没有可靠的高分锚点。"""
    covered: set[str] = set()
    for place in places.places:
        covered.update(place.scores)
    required = {"popularity", "photo", "food", "culture", "night_view", "family", "couple", "walkability"}
    missing = required - covered
    assert not missing, f"人工数据没有覆盖这些分值维度：{sorted(missing)}"


def test_curated_places_each_justify_their_existence(places: CuratedPlaceFile) -> None:
    """每条人工数据至少要能说明"它为什么在这"：提供别名、或提供说明、或覆盖评分。

    一条三者皆空的记录是纯负担 —— 建库时会去匹配一个不改变任何结果的名称。
    注意：只有 `scores` 也是合法的存在理由（用于把规则低估的地点扶到正确位置），
    所以这里不能要求"必须有别名或说明"（第一版断言就是这么写错的）。
    """
    dead_weight = [
        place.name
        for place in places.places
        if not place.aliases and not place.note and not place.scores
    ]
    assert not dead_weight, f"以下人工数据既无别名、无说明、也无评分覆盖，属于纯负担：{dead_weight}"


def test_curated_places_provide_alias_coverage(places: CuratedPlaceFile) -> None:
    """别名覆盖是这份文件的核心价值之一（机器无法从 OSM 推导出「小蛮腰」就是广州塔）。"""
    with_alias = sum(1 for place in places.places if place.aliases)
    assert with_alias >= 25, f"只有 {with_alias} 条人工数据提供了别名，别名覆盖不足"


def test_curated_places_provide_score_overrides(places: CuratedPlaceFile) -> None:
    """评分覆盖必须占多数：规则的类别基线留了上升空间，靠人工把真正的地标扶上去。"""
    with_scores = sum(1 for place in places.places if place.scores)
    assert with_scores >= 40, f"只有 {with_scores} 条人工数据提供了评分覆盖"


# ── 路线：站点必须真实、可解析、且不重复引用同一地点 ────────────────────────


def test_routes_have_at_least_three_stops(routes: CuratedRouteFile) -> None:
    thin = [route.slug for route in routes.routes if len(route.stops) < 3]
    assert not thin, f"少于 3 站的伪路线：{thin}"


def test_route_slugs_are_unique(routes: CuratedRouteFile) -> None:
    slugs = [route.slug for route in routes.routes]
    duplicates = {slug for slug, count in Counter(slugs).items() if count > 1}
    assert not duplicates, f"路线 slug 重复：{sorted(duplicates)}"


def test_route_names_are_unique(routes: CuratedRouteFile) -> None:
    """路线名重复会让用户在结果页无法区分两套方案。"""
    names = [route.name for route in routes.routes]
    duplicates = {name for name, count in Counter(names).items() if count > 1}
    assert not duplicates, f"路线名称重复：{sorted(duplicates)}"


def test_route_stop_names_are_unique_within_route(routes: CuratedRouteFile) -> None:
    for route in routes.routes:
        names = [stop.name for stop in route.stops]
        assert len(set(names)) == len(names), f"{route.slug} 内部有重复站点：{names}"


def test_route_stop_names_do_not_look_like_placeholders(routes: CuratedRouteFile) -> None:
    """站点名不能是 TODO/待定 之类的占位符 —— 那会变成一条无法执行的路线。"""
    banned = ("TODO", "待定", "待补", "占位", "xxx", "XXX", "??")
    for route in routes.routes:
        for stop in route.stops:
            assert not any(token in stop.name for token in banned), (
                f"{route.slug} 的站点 {stop.name!r} 看起来是占位符"
            )


def test_route_times_are_ordered(routes: CuratedRouteFile) -> None:
    """推荐结束时间必须晚于开始时间，否则时间线会出现负时长。"""
    for route in routes.routes:
        if route.recommended_start_time and route.recommended_end_time:
            assert route.recommended_end_time > route.recommended_start_time, (
                f"{route.slug} 的推荐时间区间反了："
                f"{route.recommended_start_time} → {route.recommended_end_time}"
            )


def test_route_archetype_distribution_is_balanced(routes: CuratedRouteFile) -> None:
    """三种 archetype 都要有足够模板，否则某一类方案只能从零组合（更慢更贵）。"""
    counts = Counter(route.archetype_hint for route in routes.routes)
    for archetype in ("relaxed", "classic", "themed"):
        assert counts.get(archetype, 0) >= 5, (
            f"archetype {archetype} 只有 {counts.get(archetype, 0)} 条模板，建议至少 5 条"
        )


def test_route_types_are_diverse(routes: CuratedRouteFile) -> None:
    kinds = Counter(route.route_type for route in routes.routes)
    assert len(kinds) >= 8, f"路线主题只有 {len(kinds)} 种，覆盖不足：{dict(kinds)}"


# ── 路线站点与人工地点的关系（自洽性，不需要数据库）────────────────────────


def test_most_route_stops_have_a_curated_entry_or_are_known_osm_places(
    places: CuratedPlaceFile, routes: CuratedRouteFile
) -> None:
    """路线站点应当尽量来自人工确认过的地点池。

    为什么这条只做"比例"断言：路线里的站点可以是 OSM 里抓到的真实地点（如「广东科学中心」），
    不强制全部来自人工清单。但如果比例过低，说明路线是凭记忆手写的、
    没有和知识库对齐过 —— 那就会大量出现"解析不到站点"被跳过的情况（实测踩过这个坑）。
    """
    curated_names = {place.name for place in places.places}
    curated_names.update(alias for place in places.places for alias in place.aliases)

    stops = [stop.name for route in routes.routes for stop in route.stops]
    hit = sum(1 for name in stops if name in curated_names)
    ratio = hit / len(stops)
    assert ratio >= 0.45, (
        f"只有 {ratio:.0%} 的路线站点出现在人工地点清单里（{hit}/{len(stops)}）。"
        "路线应与知识库对齐，否则建库时会有大量站点解析不到。"
    )


def test_seed_config_maps_every_curated_category(places: CuratedPlaceFile) -> None:
    """人工数据用到的每个类别，都必须在 config/seed.yaml 里有分值基线。

    否则该地点会因为缺少基线而在打分时出错（这是"配置与数据脱节"的典型症状）。
    """
    seed = get_seed_config()
    used = {place.category for place in places.places if place.category is not None}
    missing = used - set(seed.score_bases)
    assert not missing, f"这些类别在 seed.yaml 的 score_bases 里没有定义：{sorted(missing)}"
