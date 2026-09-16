#!/usr/bin/env python3
"""广州知识库建库脚本（幂等）。

数据流：
    data/raw/osm_guangzhou_raw.json (+ transport)
      → enrichment（config/seed.yaml 的规则）
      → 相关性过滤 / 重复合并 / 分店上限
      → 人工覆盖（data/curated/guangzhou_places.yaml）
      → places + place_aliases + place_sources + travel_sources
    data/curated/guangzhou_routes.yaml
      → 解析站点名 → 计算估算时长/步行距离 → routes + route_places
      → 写 kb_version 与 docs/DATA_REPORT.md

设计原则：
1. **幂等**：重复运行结果一致（先清空该城市的数据再重建）。
2. **可解释**：每一条被丢弃的记录都计入 drop_reasons，报告里必须能回答
   "抓了 10279 条，为什么只入库 N 条"。
3. **不编造**：解析不到的路线站点、匹配不到的人工地点都会出现在报告的
   unresolved 列表里，而**不会**被偷偷删掉或用别的地点顶上。

用法：
    python scripts/seed_guangzhou.py --dry-run     # 只分析不写库
    python scripts/seed_guangzhou.py               # 正式建库
    python scripts/seed_guangzhou.py --force       # 即使已存在行程记录也重建
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_limits_config, get_seed_config, get_settings
from app.core.logging import get_logger, setup_logging
from app.db.models import (
    City,
    KbVersion,
    Place,
    PlaceAlias,
    PlaceSource,
    Route,
    RoutePlace,
    TravelSource,
)
from app.db.session import get_sessionmaker
from app.domain.enrichment import EnrichedPlace, EnrichOutcome, OsmRecord, enrich_or_reason
from app.domain.geo import CityBoundary, haversine_m, route_factor, travel_minutes
from app.domain.naming import normalize_name, similarity
from app.schemas.curated import (
    CuratedPlaceFile,
    CuratedRouteFile,
    load_curated_places,
    load_curated_routes,
)

log = get_logger("seed")

CITY_SLUG = "guangzhou"
MATCH_THRESHOLD = 0.82
CURATED_SOURCE_NAME = "TripDecider 编辑整理"


# ════════════════════════════════════════════════════════════════════════════
# 第一步：读原始数据并丰富化
# ════════════════════════════════════════════════════════════════════════════


@dataclass
class Candidate:
    place: EnrichedPlace
    record: OsmRecord
    dropped_duplicate: bool = False


@dataclass
class SeedStats:
    raw_records: int = 0
    drop_reasons: Counter[str] = field(default_factory=Counter)
    enriched: int = 0
    duplicate_merged: int = 0
    branch_capped: int = 0
    curated_applied: int = 0
    curated_unresolved: list[str] = field(default_factory=list)
    curated_no_apply_fields: list[str] = field(default_factory=list)
    routes_total: int = 0
    route_stop_unresolved: dict[str, list[str]] = field(default_factory=dict)
    places_written: int = 0
    aliases_written: int = 0
    sources_written: int = 0
    routes_written: int = 0
    routes_skipped: dict[str, str] = field(default_factory=dict)
    category_counts: Counter[str] = field(default_factory=Counter)
    verification_counts: Counter[str] = field(default_factory=Counter)

    def as_dict(self) -> dict[str, object]:
        return {
            "raw_records": self.raw_records,
            "enriched": self.enriched,
            "duplicate_merged": self.duplicate_merged,
            "branch_capped": self.branch_capped,
            "curated_applied": self.curated_applied,
            "curated_unresolved": self.curated_unresolved,
            "places_written": self.places_written,
            "aliases_written": self.aliases_written,
            "sources_written": self.sources_written,
            "routes_written": self.routes_written,
            "drop_reasons": dict(self.drop_reasons),
            "category_counts": dict(self.category_counts),
            "verification_counts": dict(self.verification_counts),
        }


def load_raw_records(raw_dir: Path, stats: SeedStats) -> list[OsmRecord]:
    records: list[OsmRecord] = []
    seen: set[tuple[str, int]] = set()
    files = sorted(raw_dir.glob("osm_guangzhou_*.json"))
    if not files:
        raise SystemExit(
            f"未找到原始数据（{raw_dir}/osm_guangzhou_*.json）。\n先运行：make fetch-osm"
        )
    for path in files:
        payload = json.loads(path.read_text(encoding="utf-8"))
        file_records = payload.get("records") or []
        kept = 0
        for raw in file_records:
            record = OsmRecord.from_raw(raw)
            key = (record.osm_type, record.osm_id)
            if key in seen:
                continue
            seen.add(key)
            records.append(record)
            kept += 1
        print(f"  读取 {path.name}：{kept} 条（累计 {len(records)}）")
    stats.raw_records = len(records)
    return records


def curated_protected_names(curated: CuratedPlaceFile) -> frozenset[str]:
    """人工整理地点的保护名单（名称 + 别名，均归一化）。

    这些名字会跳过相关性过滤 —— 机器规则会误杀真实地标（实测：广州塔、白云山、
    荔湾湖公园都曾被规则丢弃），人工确认过的必须让规则让路。
    """
    names: set[str] = set()
    for entry in curated.places:
        names.add(normalize_name(entry.name))
        names.update(normalize_name(alias) for alias in entry.aliases)
    names.discard("")
    return frozenset(names)


def load_city_boundary(slug: str = CITY_SLUG) -> CityBoundary | None:
    """加载城市行政边界（由 scripts/fetch_city_boundary.py 生成）。

    找不到时返回 None 并告警 —— 此时退化为"地址标签 + 名称前缀"过滤，
    会漏掉无标签的越界地点（实测漏掉过深圳的锦绣中华民俗村）。
    """
    from app.core.paths import data_dir

    path = Path(data_dir()) / "reference" / f"{slug}_boundary.json"
    if not path.exists():
        print(f"   ⚠︎ 未找到城市边界 {path}，将退化为地址/名称过滤（可能漏掉无标签的越界地点）")
        print("     修复：python scripts/fetch_city_boundary.py")
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    boundary = CityBoundary.from_payload(payload)
    print(f"   已加载 {slug} 行政边界（{boundary.point_count} 个点，来自 OSM relation "
          f"{payload.get('meta', {}).get('osm_relation_id')}）")
    return boundary


def enrich_all(
    records: list[OsmRecord],
    stats: SeedStats,
    protected: frozenset[str] = frozenset(),
    boundary: CityBoundary | None = None,
) -> list[Candidate]:
    cfg = get_seed_config()
    candidates: list[Candidate] = []
    for record in records:
        outcome: EnrichOutcome = enrich_or_reason(
            record, cfg, protected_names=protected, boundary=boundary
        )
        if outcome.place is None:
            stats.drop_reasons[str(outcome.reason)] += 1
            continue
        candidates.append(Candidate(place=outcome.place, record=record))
    stats.enriched = len(candidates)
    return candidates


# ════════════════════════════════════════════════════════════════════════════
# 第二步：重复合并 + 分店上限
# ════════════════════════════════════════════════════════════════════════════


def _quality_key(candidate: Candidate) -> tuple[int, int]:
    """信息量排序键：可核验信号数 → 原始标签数。越大越值得保留。"""
    return (candidate.place.signal_count, candidate.place.raw_tag_count)


def merge_duplicates(candidates: list[Candidate], stats: SeedStats) -> list[Candidate]:
    """同名且相距很近的记录视为同一地点被重复测绘（node + way 各一份），只留信息量最多的一条。"""
    cfg = get_seed_config()
    radius = cfg.relevance.duplicate_merge_radius_m
    groups: dict[str, list[Candidate]] = defaultdict(list)
    for candidate in candidates:
        groups[normalize_name(candidate.place.canonical_name)].append(candidate)

    kept: list[Candidate] = []
    for _name, group in groups.items():
        if len(group) == 1:
            kept.append(group[0])
            continue
        used = [False] * len(group)
        for i, base in enumerate(group):
            if used[i]:
                continue
            cluster = [base]
            used[i] = True
            for j in range(i + 1, len(group)):
                if used[j]:
                    continue
                other = group[j]
                distance = haversine_m(
                    base.place.latitude, base.place.longitude,
                    other.place.latitude, other.place.longitude,
                )
                if distance <= radius:
                    cluster.append(other)
                    used[j] = True
            cluster.sort(key=_quality_key, reverse=True)
            kept.append(cluster[0])
            stats.duplicate_merged += len(cluster) - 1
    return kept


def cap_branches(candidates: list[Candidate], stats: SeedStats) -> list[Candidate]:
    """同名地点超过上限时，只保留信息量最强的 N 个（避免几百家瑞幸式灌水）。

    注意：这一步是在**商品连锁品牌已经被丢弃**之后才执行的，
    它处理的是"品牌有真实旅行价值但分店过多"的情况（如点都德 12 家分店）。
    """
    cfg = get_seed_config()
    cap = cfg.relevance.max_branches_per_brand
    center = cfg.coord.city_center
    groups: dict[str, list[Candidate]] = defaultdict(list)
    for candidate in candidates:
        groups[normalize_name(candidate.place.canonical_name)].append(candidate)

    kept: list[Candidate] = []
    for _name, group in groups.items():
        if len(group) <= cap:
            kept.extend(group)
            continue
        group.sort(
            key=lambda c: (
                -c.place.signal_count,
                -c.place.raw_tag_count,
                haversine_m(center["lat"], center["lng"], c.place.latitude, c.place.longitude),
            )
        )
        kept.extend(group[:cap])
        stats.branch_capped += len(group) - cap
    return kept


# ════════════════════════════════════════════════════════════════════════════
# 第三步：人工覆盖
# ════════════════════════════════════════════════════════════════════════════


class PlaceIndex:
    """按规范名 **与别名** 查找地点，支持模糊匹配。

    为什么必须查别名：OSM 的命名与常识命名经常不一致 ——
    「太古仓」在 OSM 里叫「太古仓码头」、「银记肠粉店」叫「银记肠粉」、
    「粤海关博物馆」叫「粤海关旧址（中国海关博物馆广州分馆）」。
    第一版实现只查规范名，结果 44 条路线里有 23 条因为"站点解析不到"被跳过；
    这些地点其实**都在库里**，只是名字对不上。别名表正是为这种情况准备的。
    """

    def __init__(self, candidates: list[Candidate]) -> None:
        self._by_norm: dict[str, list[Candidate]] = defaultdict(list)
        self._candidates = candidates
        self._search_names: dict[int, list[str]] = {}
        for candidate in candidates:
            place = candidate.place
            names = {normalize_name(place.canonical_name)}
            names.update(normalize_name(alias) for alias in place.aliases)
            names.discard("")
            self._search_names[id(candidate)] = sorted(names)
            for name in names:
                self._by_norm[name].append(candidate)

    def find(self, names: list[str]) -> Candidate | None:
        for name in names:
            hits = self._by_norm.get(normalize_name(name))
            if hits:
                return max(hits, key=_quality_key)
        # 模糊匹配：既比对规范名，也比对别名（阈值与实体匹配策略一致）
        best: tuple[float, Candidate] | None = None
        for name in names:
            for candidate in self._candidates:
                for candidate_name in self._search_names.get(id(candidate), []):
                    score = similarity(name, candidate_name)
                    if score >= MATCH_THRESHOLD and (best is None or score > best[0]):
                        best = (score, candidate)
        return best[1] if best else None


def apply_curated_places(
    candidates: list[Candidate], curated: CuratedPlaceFile, stats: SeedStats
) -> None:
    """把人工整理的别名与编辑性评分覆盖到匹配到的地点上。

    匹配不到的人工地点会被如实记录在 ``curated_unresolved`` 里。
    这里**不会**凭人工数据凭空创建地点 —— 没有可核验来源的地点不入库。
    """
    index = PlaceIndex(candidates)
    for entry in curated.places:
        found = index.find([entry.name, *entry.aliases])
        if found is None:
            stats.curated_unresolved.append(entry.name)
            continue
        stats.curated_applied += 1
        place = found.place

        merged_scores = dict(place.scores)
        merged_scores.update(entry.scores)
        extra_tags = tuple(t for t in entry.tags if t not in place.tags)
        aliases = set(place.aliases)
        for alias in entry.aliases:
            if normalize_name(alias) != normalize_name(place.canonical_name):
                aliases.add(alias)
        # 人工数据里 name 本身与 OSM 名称不同时，把 OSM 名保留为别名
        if normalize_name(entry.name) != normalize_name(place.canonical_name):
            aliases.add(entry.name)

        flags = tuple(
            dict.fromkeys(
                (*(f for f in place.data_quality_flags if f != "score_derived"), "score_curated")
            )
        )
        found.place = replace_place(
            place,
            category=entry.category or place.category,
            recommended_duration_min=entry.recommended_duration_min or place.recommended_duration_min,
            scores=merged_scores,
            tags=(*place.tags, *extra_tags),
            aliases=tuple(sorted(aliases)),
            data_quality_flags=flags,
        )


def replace_place(place: EnrichedPlace, **changes: object) -> EnrichedPlace:
    from dataclasses import replace

    return replace(place, **changes)  # type: ignore[arg-type]


# ════════════════════════════════════════════════════════════════════════════
# 第四步：写库
# ════════════════════════════════════════════════════════════════════════════


async def ensure_city(session: AsyncSession) -> City:
    existing = (await session.execute(select(City).where(City.slug == CITY_SLUG))).scalar_one_or_none()
    if existing:
        return existing
    city = City(
        slug=CITY_SLUG,
        name="广州",
        name_en="Guangzhou",
        country="CN",
        province="广东省",
        description="广东省省会，岭南文化中心，以早茶、骑楼老街与珠江夜景闻名。",
        timezone="Asia/Shanghai",
        centroid_lat=23.1291,
        centroid_lng=113.2644,
        bbox_min_lat=22.50,
        bbox_max_lat=23.95,
        bbox_min_lng=112.90,
        bbox_max_lng=114.05,
        status="seeding",
    )
    session.add(city)
    await session.flush()
    return city


async def clear_city_data(session: AsyncSession, city: City, *, force: bool) -> None:
    """清空该城市的派生数据，保证脚本幂等。"""
    trip_refs = (
        await session.execute(
            text(
                "SELECT count(*) FROM trip_route_stops s JOIN places p ON p.id = s.place_id WHERE p.city_id = :c"
            ),
            {"c": city.id},
        )
    ).scalar_one()
    if trip_refs and not force:
        raise SystemExit(
            f"已有 {trip_refs} 条行程记录引用本地点的数据；重建会破坏它们。\n"
            "确认要重建请加 --force（会先删除相关行程）。"
        )
    if trip_refs:
        await session.execute(text("DELETE FROM trips WHERE city_id = :c"), {"c": city.id})
    await session.execute(delete(RoutePlace).where(RoutePlace.route_id.in_(select(Route.id).where(Route.city_id == city.id))))
    await session.execute(delete(Route).where(Route.city_id == city.id))
    await session.execute(delete(PlaceSource).where(PlaceSource.place_id.in_(select(Place.id).where(Place.city_id == city.id))))
    await session.execute(delete(PlaceAlias).where(PlaceAlias.city_id == city.id))
    await session.execute(delete(Place).where(Place.city_id == city.id))
    await session.flush()


async def get_or_create_source(session: AsyncSession, *, url: str, name: str, credibility: float) -> TravelSource:
    from hashlib import sha256

    url_hash = sha256(url.encode("utf-8")).hexdigest()
    existing = (
        await session.execute(select(TravelSource).where(TravelSource.url_hash == url_hash))
    ).scalar_one_or_none()
    if existing:
        return existing
    source = TravelSource(
        source_type="map_api",
        source_name=name,
        url=url,
        url_hash=url_hash,
        domain="www.openstreetmap.org",
        title="OpenStreetMap 元素",
        language="zh",
        credibility_score=credibility,
    )
    session.add(source)
    await session.flush()
    return source


async def write_places(
    session: AsyncSession, city: City, candidates: list[Candidate], stats: SeedStats
) -> None:
    """写入 places 及其别名、来源。路线通过 external_id 回查 place.id。"""
    for candidate in candidates:
        p = candidate.place
        scores = p.to_scores_payload()
        row = Place(
            city_id=city.id,
            canonical_name=p.canonical_name,
            display_name=p.display_name,
            name_en=p.name_en,
            category=p.category,
            district=p.district,
            latitude=p.latitude,
            longitude=p.longitude,
            address=p.address,
            recommended_duration_min=p.recommended_duration_min,
            opening_hours_raw=p.opening_hours_raw,
            # opening_hours 结构化字段**留空**：MVP 尚未实现 OSM opening_hours 语法解析，
            # 与其塞入半吊子的解析结果，不如留 NULL 并在 unknown_fields 里说明。
            best_time=list(p.best_time),
            indoor=p.indoor,
            tags=list(p.tags),
            source_url=p.source_url,
            source_name=p.source_name,
            verification_status=p.verification_status,
            confidence=p.confidence,
            unknown_fields=[*p.unknown_fields, "opening_hours_structured"],
            data_quality_flags=list(p.data_quality_flags),
            status="active",
            external_source=p.external_source,
            external_id=p.external_id,
            **scores,
        )
        session.add(row)
        await session.flush()

        source = await get_or_create_source(
            session, url=p.source_url, name=p.source_name, credibility=p.source_credibility
        )
        session.add(
            PlaceSource(place_id=row.id, source_id=source.id, field_scope=list(p.source_field_scope))
        )
        row.primary_source_id = source.id
        stats.sources_written += 1

        # 按归一化形式去重：两个不同写法的别名可能归一化成同一个键
        # （例如「粤海关旧址」与「粤海关旧址（中国海关博物馆广州分馆）」），
        # 而 (place_id, alias_norm) 是唯一约束，不去重会直接插入失败。
        seen_alias_norms: set[str] = set()
        for alias in p.aliases:
            alias_norm = normalize_name(alias)
            if not alias_norm or alias_norm in seen_alias_norms:
                continue
            seen_alias_norms.add(alias_norm)
            session.add(
                PlaceAlias(
                    place_id=row.id,
                    city_id=city.id,
                    alias=alias,
                    alias_norm=alias_norm,
                    alias_type="zh_variant" if not alias.isascii() else "en",
                    source="derived",
                    confidence=0.9,
                )
            )
            stats.aliases_written += 1

        stats.places_written += 1
        stats.category_counts[p.category] += 1
        stats.verification_counts[p.verification_status] += 1


async def write_routes(
    session: AsyncSession,
    city: City,
    curated: CuratedRouteFile,
    candidates: list[Candidate],
    stats: SeedStats,
) -> None:
    """写入路线模板。站点名解析不到的路线会被**跳过并记录原因**，不做降级填充。"""
    limits = get_limits_config()
    index = PlaceIndex(candidates)

    for route in curated.routes:
        resolved: list[tuple[Candidate, int, str]] = []
        unresolved: list[str] = []
        for stop in route.stops:
            found = index.find([stop.name])
            if found is None:
                unresolved.append(stop.name)
            else:
                resolved.append((found, stop.stay_min or 60, stop.note))

        if unresolved:
            stats.route_stop_unresolved[route.slug] = unresolved
        if len(resolved) < 3:
            stats.routes_skipped[route.slug] = (
                f"仅解析到 {len(resolved)} 个站点（未解析：{unresolved}），少于 3 站不入库"
            )
            continue

        # ── 估算路线指标（全部标记为估算，见下方注释）──
        total_stay = sum(stay for _, stay, _ in resolved)
        walking_m = 0
        transit_min = 0
        for i in range(len(resolved) - 1):
            a, b = resolved[i][0].place, resolved[i + 1][0].place
            crow = haversine_m(a.latitude, a.longitude, b.latitude, b.longitude)
            distance = crow * route_factor(crow)
            mode = "walk" if distance <= 1500 else ("metro" if distance <= 8000 else "taxi")
            spec = limits.travel_modes[mode]
            transit_min += travel_minutes(distance, spec.speed_kmh, overhead_min=spec.overhead_min)
            if mode == "walk":
                walking_m += int(distance)
        duration = total_stay + transit_min

        # 预算**不估算**：我们没有可靠的餐饮/门票价格来源，宁可留空并由 UI 明说
        # "预算待确认"，也不用编造的数字冒充。见 docs/DATA_REPORT.md。
        row = Route(
            city_id=city.id,
            slug=route.slug,
            name=route.name,
            description=route.description,
            route_type=route.route_type,
            archetype_hint=route.archetype_hint,
            difficulty=route.difficulty,
            pace=route.pace,
            duration_min=duration,
            walking_distance_m=walking_m,
            estimated_transport_time_min=transit_min,
            recommended_start_time=route.recommended_start_time,
            recommended_end_time=route.recommended_end_time,
            best_for=list(route.best_for),
            score=None,
            is_template=True,
            source_name=CURATED_SOURCE_NAME,
            verification_status="unknown",
        )
        session.add(row)
        await session.flush()

        for seq, (candidate, stay, note) in enumerate(resolved):
            next_info = None
            if seq < len(resolved) - 1:
                a, b = candidate.place, resolved[seq + 1][0].place
                crow = haversine_m(a.latitude, a.longitude, b.latitude, b.longitude)
                distance = int(crow * route_factor(crow))
                mode = "walk" if distance <= 1500 else ("metro" if distance <= 8000 else "taxi")
                spec = limits.travel_modes[mode]
                next_info = (
                    mode,
                    travel_minutes(distance, spec.speed_kmh, overhead_min=spec.overhead_min),
                    distance,
                )
            session.add(
                RoutePlace(
                    route_id=row.id,
                    place_id=await _place_id_for(session, city, candidate),
                    seq=seq,
                    stay_min=stay,
                    note=note,
                    transport_to_next=next_info[0] if next_info else None,
                    transport_to_next_min=next_info[1] if next_info else None,
                    distance_to_next_m=next_info[2] if next_info else None,
                )
            )
        stats.routes_written += 1


async def _place_id_for(session: AsyncSession, city: City, candidate: Candidate) -> object:
    """按 external_id 找已写入的 place.id。"""
    from app.db.models import Place as PlaceModel

    place_id = (
        await session.execute(
            select(PlaceModel.id).where(
                PlaceModel.city_id == city.id,
                PlaceModel.external_id == candidate.place.external_id,
            )
        )
    ).scalar_one_or_none()
    if place_id is None:
        raise RuntimeError(f"地点 {candidate.place.canonical_name} 未写入数据库，无法建立路线引用")
    return place_id


# ════════════════════════════════════════════════════════════════════════════
# 报告
# ════════════════════════════════════════════════════════════════════════════


def compute_kb_version(candidates: list[Candidate], routes: CuratedRouteFile) -> str:
    """内容寻址的知识库版本：数据不变则版本不变，数据一变版本必变。

    摘要内容包含地点身份、类别、坐标（6 位小数）、分值、可信度与路线站点序列 ——
    足以捕捉任何会影响规划结果的变更。
    """
    import hashlib

    snapshot = {
        "places": sorted(
            f"{c.place.canonical_name}|{c.place.category}|{c.place.latitude:.6f}|"
            f"{c.place.longitude:.6f}|{c.place.scores.get('popularity', 0):.3f}|"
            f"{c.place.verification_status}"
            for c in candidates
        ),
        "routes": sorted(
            f"{r.slug}|" + ",".join(f"{stop.name}:{stop.stay_min}" for stop in r.stops)
            for r in routes.routes
        ),
    }
    digest = hashlib.sha256(
        json.dumps(snapshot, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:10]
    return f"gz-{datetime.now(UTC).strftime('%Y.%m.%d')}-{digest}"


def relation_network_lines() -> list[str]:
    """关系图那一段文案：**只讲结构，不抄数字**。

    这段文案先是写死了「本次 5445 对里 163 对」，后来重算成 161、再重算成 146 ——
    三次的地点集合与候选对数完全一样，变的只是**分批边界**（当时批次是按 UUID 排序切的，
    现已改为按候选关系图聚类，见 `compute_relations.cluster_batches`），
    一次网络失败也没有。抄数字的文案永远追不上重算，而结构性事实（table 只算批内矩阵）
    是稳定的。所以：这里只讲结构与后果，**具体数字只留在 `docs/RELATION_REPORT.json` 一处**
    （它现在自带 `osrm_eligible_pairs` / `osrm_batches_failed` / `generated_at`）。

    读一眼那份报告只为回答一件事：当前的关系图是真实路网还是离线估算模式生成的
    （`use_osrm`）—— 这是"这段话该怎么读"的前提，而不是一个会漂的数字。
    """
    from app.core.paths import docs_dir

    head = "8. **关系图的真实路网占比有限，而且这个上限是结构性的**："
    pointer = (
        "   具体覆盖对数、失败批次数与生成时间见 `docs/RELATION_REPORT.json`"
        "（`make relations OSRM=1` 产出）—— 这份数据报告不抄那几个数字，"
        "因为它们会随重算变化，而抄来的数只会变成一处过期副本。"
    )
    path = docs_dir() / "RELATION_REPORT.json"
    if not path.exists():
        mode = "（当前还没有关系图报告：先跑 `make relations OSRM=1`。）"
    else:
        payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        mode = (
            "（当前这份是真实路网模式生成的。）"
            if payload.get("use_osrm")
            else "（⚠︎ 当前这份是**离线估算**模式生成的：先跑 `make relations OSRM=1` 再来读。）"
        )
    # 编号列表的续行必须缩进，否则这一行会把清单切断，后面几行会变成一个缩进代码块。
    mode = "   " + mode
    return [
        head + "OSRM 的 `table` 接口一次只算一批（`batch_size`）内部的矩阵，",
        "   因此跳批次的候选对**必然**退回估算 —— 覆盖率的上限是分批结构，不是网络好坏。",
        "   分批方式本身就是一个可调的杠杆：按 UUID 切块（与地理位置无关）时只有 2.4% 的候选对同批；",
        "   改成按候选关系图做贪心聚类（`cluster_batches`）后同批比例提到 75%，没有换接口也没有调 `batch_size`。",
        mode,
        "   估算值逐条带 `data_source` 与 `derived_modes` 标注，从不冒充实测；",
        "   要再往上提高就只能自建 OSRM、减小 `batch_size`（请求数上升）或改用商业地图服务。",
        pointer,
        "",
    ]


def build_report(stats: SeedStats, kb_version: str, quality: dict[str, object] | None = None) -> str:
    total_dropped = sum(stats.drop_reasons.values())
    lines = [
        "# 广州知识库数据报告",
        "",
        f"- 生成时间：{datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S UTC')}",
        f"- 知识库版本：`{kb_version}`",
        f"- 原始 OSM 记录：**{stats.raw_records}**",
        f"- 通过丰富化：**{stats.enriched}**",
        f"- 合并重复测绘：{stats.duplicate_merged}",
        f"- 分店上限裁减：{stats.branch_capped}",
        f"- **最终入库地点：{stats.places_written}**",
        f"- 别名：{stats.aliases_written} · 来源条目：{stats.sources_written}",
        f"- **路线：{stats.routes_written} / {stats.routes_total}**",
        "",
        "## 为什么 10279 条原始记录只入库了这么多？",
        "",
        "每一步过滤的原因与数量如下（**如实列出，不做美化**）：",
        "",
        "| 丢弃原因 | 数量 | 说明 |",
        "| --- | ---: | --- |",
    ]
    reasons = {
        "commodity_chain": "通用商品连锁品牌分店（瑞幸/星巴克/麦当劳等）—— 对「去哪玩」没有决策价值",
        "dropped_primary_tag": "生活类/过细碎要素（河涌、无名山头、社区中心、街头雕塑等）",
        "missing_verifiable_signal": "所属类别要求至少一个可核验信号（公园/商场），该条无任何信号",
        "duplicate_merged": "同名且 150m 内的重复测绘（node/way 各一份）",
        "name_too_short": "名称过短",
        "meaningless_name": "名称无信息量（纯数字/符号）",
        "blocklisted_name": "名称在黑名单里（公厕、停车场等）",
        "coord_out_of_bbox": "坐标落在广州 bbox 之外",
        "unknown_category": "无法映射到任何已知类别",
        "outside_city_boundary": "落在城市行政边界之外（矩形 bbox 切进了邻市）——权威判据",
        "outside_city": "OSM 地址标签写明的邻市地点（深圳/东莞/佛山/江门等）——边界不可用时的兜底",
        "outside_city_by_name": "名称以邻市名开头（东莞/佛山/深圳/中山市）——边界不可用时的兜底",
    }
    for reason, count in stats.drop_reasons.most_common():
        lines.append(f"| `{reason}` | {count} | {reasons.get(reason, '')} |")
    lines.append(f"| **合计丢弃** | **{total_dropped}** | 占原始记录的 {total_dropped / max(stats.raw_records, 1) * 100:.1f}% |")

    lines += [
        "",
        "## 入库地点的类别分布",
        "",
        "| 类别 | 数量 |",
        "| --- | ---: |",
    ]
    lines += [f"| {cat} | {n} |" for cat, n in stats.category_counts.most_common()]

    lines += [
        "",
        "## 身份可信度分布",
        "",
        "> `verified` 的含义**仅限身份可信**（该实体在 OSM 上带 wikidata/wikipedia 标签，可交叉核验）。",
        "> 它**不代表营业时间与价格已核实** —— 这两项绝大多数为 `unknown`，见下节。",
        "",
        "| verification_status | 数量 |",
        "| --- | ---: |",
    ]
    lines += [f"| {status} | {n} |" for status, n in stats.verification_counts.most_common()]

    if stats.curated_unresolved:
        lines += [
            "",
            "## ⚠️ 人工整理但未能匹配的地点（**未入库，也没有编造来源**）",
            "",
            "这些地点在人工数据里被列出，但在 OSM 抓取结果里找不到可核验的记录。",
            "按「宁可 unknown 不可猜测」的原则，它们没有被凭空插入数据库：",
            "",
        ]
        lines += [f"- {name}" for name in stats.curated_unresolved]

    if stats.route_stop_unresolved:
        lines += ["", "## ⚠️ 路线中未能解析的站点", ""]
        for slug, names in stats.route_stop_unresolved.items():
            lines.append(f"- `{slug}`：{'、'.join(names)}")

    if stats.routes_skipped:
        lines += ["", "## ⚠️ 被跳过的路线（不足 3 个可解析站点）", ""]
        lines += [f"- `{slug}`：{reason}" for slug, reason in stats.routes_skipped.items()]

    if quality:
        lines += ["", "## 质检结果", "", "```json", json.dumps(quality, ensure_ascii=False, indent=2), "```"]

    lines += [
        "",
        "## ⚠️ 一次重要的数据修正：用行政边界取代矩形 bbox",
        "",
        "整城抓取最初用的是**矩形 bbox**（22.50–23.95N, 112.90–114.05E，约 18 800 km²）。",
        "但广州的实际行政面积只有约 **7 434 km²** —— 也就是说矩形里有**超过一半不是广州**，",
        "而是东莞、深圳、佛山、中山、惠州、清远的一部分。后果是「深圳野生动物园」",
        "「东莞博物馆」「锦绣中华民俗村」都进了广州知识库，还因为信号多、热度分值高排到了列表最前面。",
        "",
        "修正过程（三步，每步都留下了记录）：",
        "",
        "1. 先用**可核验的证据**过滤：OSM 的 `addr:city` / `addr:district` 写明的邻市地点，",
        "   以及名称以邻市名开头的地点 —— 但这只能拦下 369 条，且**不能**用裸「中山」前缀，",
        "   否则「中山纪念堂」「国立中山大学旧址」这类广州本地地标会被成批误删。",
        "2. 仍然漏掉了「锦绣中华民俗村」（深圳华侨城）这种**既无地址标签、名称也不含城市名**的地点。",
        "3. 因此抓取广州市的**行政边界**（OSM relation 3287346，5059 个点）做点面判定 ——",
        "   这是唯一可靠的判据。边界文件见 `backend/data/reference/guangzhou_boundary.json`，",
        "   判定逻辑是纯函数 `app/domain/geo.py::point_in_rings`，离线可用。",
        "",
        "**影响：入库地点从 3 776 条降到本轮的规模。被剔除的都是其他城市的地点，**",
        "**不是广州的数据变少了。** 这正是「宁可说少，也不虚报」的实践 ——",
        "在此之前，知识库里约一半的地点根本不在广州。",
        "",
        "残留局限：边界数据本身有版本（本次抓取于报告生成前），行政区划调整后需重跑",
        "`scripts/fetch_city_boundary.py`。",
        "",
        "## 类别体系的说明（避免用错误指标衡量数据质量）",
        "",
        "- **「拍照」「CityWalk」不是地点类型，而是属性**。`places.photo_score` / `walkability_score` 是分值维度，",
        "  沙面属于 `district`、石室圣心大教堂属于 `historic`、广州塔属于 `nightview` ——",
        "  它们 photo_score 很高，但类别不是 `photo`。因此质检**不检查** `photo` 类别的数量，",
        "  该偏好由 `config/scoring.yaml` 的 `preference_dimensions.photo → field: photo_score` 承载，",
        "  「CityWalk」由 `walkability_score` 承载。因此质检**不检查** `photo` 与 `citywalk` 类别的数量。",
        "- `district` / `citywalk` 数量曾长期偏低，结构性原因：它们主要来自 OSM 的 `place=*`",
        "  类目（suburb / neighbourhood / island / village），而该类目**不在主抓取的类别过滤器里**。",
        "  **2026-09-15 已通过定向补抓补上，算法一行未改**（见下第 0 条）。",
        "",
        "## 已知数据缺口（明确记录，不掩盖）",
        "",
        "0. **B1/B2 两类缺口已于 2026-09-15 补齐**（此前长期记为「外部原因，未抓到」）：",
        "   - `place=*` 类目（街区/岛屿/村落）：原先过滤器不含它（当初的设计疏漏）。",
        "     由 `fetch_osm_curated_extras.py` 定向补抓：本次写入 212 条记录，",
        "     落库后 `district` 8 → 13（质检门槛 ≥ 10），此前缺失的二沙岛 / 东山口 / 小洲 /",
        "     花城广场 / 沙面街道 / 太古仓 / 琶醍 / 泮溪酒家 等已入库。",
        "   - `transport_hub`（地铁站/公交站/轮渡）：此前整块 bbox 查询稳定 HTTP 504。",
        "     2026-09-15 定位到根因**不是数据量而是查询写法** ——",
        "     正则过滤器 `[\"railway\"~\"^(station|subway_entrance)$\"]` 迫使 Overpass 走全量正则匹配；",
        "     改成逐值等值过滤后**同一 bbox 直接 200**。修复落在 `fetch_osm_guangzhou.value_filters`",
        "     （全组通用，实测数据写在该函数 docstring 里）。补抓由 `fetch_osm_transport.py` 完成：",
        "     2708 条原始记录 → 落库 1525 条 `transport_hub`，解决了「没有起点建议」这一缺口。",
        "   - 镜像可用性同时变了：`overpass.kumi.systems` 已长期连接超时，替换为 `maps.mail.ru`；",
        "     另外 6 个候选的逐个实测结果记在 `fetch_osm_guangzhou.MIRRORS` 上方的注释里。",
        "   - **仍然缺（如实记录，不编造来源）**：永庆坊、上下九步行街、北京路步行街、",
        "     广州城隍庙、海珠湖公园、粤海关博物馆 —— 这些**具体名称**在 OSM 里没有可核验记录。",
        "     注意「上下九商业步行街」「北京路商业步行街」已以另一拼写入库，缺的是名单里的那个名称。",
        "     依赖它们的人工路线仍用地理上等价的真实站点（见路线库的调整记录）。",
        "",
        "1. **营业时间**：OSM 中覆盖率很低（原始数据仅约 3.8% 有 `opening_hours` 标签）。",
        "   有值的把原文留在 `opening_hours_raw`，由 `app/domain/opening_hours.py` **读时解析**",
        "   （`opening_hours` 列仍为 NULL，缺原文的地点照旧把字段名记入 `unknown_fields` ——",
        "   解析只有一份实现、原文才是权威，多一份落库副本只会漂）。",
        "   解析器只做能保证正确的那部分：实测 509 条原文解得出 **267 条（52.5%）**，",
        "   看不懂的（跨零点 `05:56-00:25`、脏数据 `06:06-24:08`）一律返回 None 并继续报未知。",
        "   所以要注意两个数不是同一件事：质检里的「营业时间未知」看的是 `opening_hours_raw` 有无值，",
        "   而机器可读的**时段**只有 267/3166 ≈ 8.4% —— 一个猜错的开放时间比「未知」更危险，",
        "   剩下的照旧在 UI 提示出发前确认。",
        "2. **价格**：OSM 基本不提供票价。`price_min/price_max` 几乎全为 NULL，**不用编造的票价填充**；",
        "   但**餐饮**是另一件事：路线跨过饭点、那个站点却没有价格时，用 `config/limits.yaml` 的",
        "   餐费单价推定并逐项记入 `estimated_items`（前端与估算标记一起显示）——",
        "   旧行为是把这顿饭从总额里静默抹掉，一条两顿老字号的路线会报出只含一段打车费的「预算」。",
        "3. **行政区**：`addr:district` 覆盖率低（约 4.4%），因此 `district` 大量为 NULL。",
        "   不使用「按坐标反推行政区」的近似做法 —— 那会产出看起来精确但实际可能错误的字段。",
        "4. **分值不是事实**：`*_score` 是由类别基线与真实标签信号按 `config/seed.yaml` 规则",
        "   推导的**编辑性评分**，用于排序。带 `score_derived` / `score_curated` 标记可区分来源。",
        "5. **路线时长与步行距离是估算**：由站点坐标的直线距离 × 绕行系数 + 配置里的速度假设得出，",
        "   不是真实路网结果；真实路网数据由 `scripts/compute_relations.py` 单独计算并标记 `osrm`。",
        "6. **路线预算留空**：没有可靠的餐饮/门票价格来源，不做估算填充。",
        "7. **`verified` 占比 17.1%**（PRD 初稿期望 30%）：判据是「有 wikidata/wikipedia 可交叉核验」。",
        "   2026-09-15 补抓街区与交通枢纽后，该比例由 5.3% 升到 17.1% ——",
        "   新入库的这两类记录带 wikidata/wikipedia 标签的比例显著高于原来的餐饮/公园类。",
        "   但 OSM 里该标签的整体覆盖率仍低，且 wikidata.org / zh.wikipedia.org 在本机网络**不可达**，",
        "   无法作为补充来源。因此 `config/limits.yaml` 的 `min_verified_ratio` 仍按实测能力校准在 3%，",
        "   同时保留 `aspirational_verified_ratio: 0.30` 作为**每次质检都会报出来的长期目标**——",
        "   目的是让这个缺口一直可见，而不是通过调低阈值把它藏起来。",
        "   要真正达成 30%，需要引入官方名录（广州市文旅局 A 级景区、文保单位名单等）。",
        *relation_network_lines(),
    ]
    return "\n".join(lines)


# ════════════════════════════════════════════════════════════════════════════
# 主流程
# ════════════════════════════════════════════════════════════════════════════


async def run(*, dry_run: bool, force: bool, write_report: bool = True) -> int:
    from app.core.paths import curated_data_dir, docs_dir, raw_data_dir

    setup_logging("INFO")
    stats = SeedStats()

    print("① 读取原始 OSM 数据")
    records = load_raw_records(raw_data_dir(), stats)

    curated_places = load_curated_places(curated_data_dir() / "guangzhou_places.yaml")
    protected = curated_protected_names(curated_places)
    print(f"\n② 丰富化（按 config/seed.yaml 规则）并过滤；人工保护名单 {len(protected)} 个名称")
    boundary = load_city_boundary()
    candidates = enrich_all(records, stats, protected, boundary)
    print(f"   通过 {stats.enriched} 条，丢弃 {sum(stats.drop_reasons.values())} 条")

    print("\n③ 合并重复测绘 + 分店上限")
    candidates = merge_duplicates(candidates, stats)
    candidates = cap_branches(candidates, stats)
    print(f"   合并重复 {stats.duplicate_merged} 条，分店裁减 {stats.branch_capped} 条，剩余 {len(candidates)}")

    print("\n④ 应用人工整理数据（别名 + 编辑性评分）")
    apply_curated_places(candidates, curated_places, stats)
    print(f"   匹配成功 {stats.curated_applied} / {len(curated_places.places)}")
    if stats.curated_unresolved:
        print(f"   未匹配（不会入库，也不会编造来源）：{'、'.join(stats.curated_unresolved)}")

    curated_routes = load_curated_routes(curated_data_dir() / "guangzhou_routes.yaml")
    stats.routes_total = len(curated_routes.routes)

    if dry_run:
        counts: Counter[str] = Counter(c.place.category for c in candidates)
        stats.category_counts = counts
        stats.verification_counts = Counter(c.place.verification_status for c in candidates)
        print("\n[--dry-run] 不写数据库。以下是分析结果：")
        print(json.dumps(stats.as_dict(), ensure_ascii=False, indent=2))
        return 0

    # ★ kb_version 必须**内容寻址**（而不是按日期） ★
    # 原因：plan_cache 用 (params_hash, kb_version) 作为键来做缓存失效。
    # 如果同一天重跑建库时版本号不变，那么"数据变了但缓存没失效"，
    # 用户会拿到基于旧数据的路线 —— 这是最难排查的一类问题。
    kb_version = compute_kb_version(candidates, curated_routes)

    print("\n⑤ 写入数据库")
    maker = get_sessionmaker()
    async with maker() as session:
        city = await ensure_city(session)
        await clear_city_data(session, city, force=force)
        await write_places(session, city, candidates, stats)
        await write_routes(session, city, curated_routes, candidates, stats)
        city.status = "active"
        city.coverage_score = min(1.0, stats.places_written / 300)
        # 幂等写入：内容相同则版本号相同，此时更新已有记录而不是重复插入
        existing_version = (
            await session.execute(select(KbVersion).where(KbVersion.version == kb_version))
        ).scalar_one_or_none()
        notes = (
            f"OSM 原始 {stats.raw_records} 条 → 入库 {stats.places_written} 条地点、"
            f"{stats.routes_written} 条路线"
        )
        if existing_version is None:
            session.add(
                KbVersion(
                    version=kb_version,
                    city_id=city.id,
                    place_count=stats.places_written,
                    route_count=stats.routes_written,
                    notes=notes,
                )
            )
        else:
            existing_version.place_count = stats.places_written
            existing_version.route_count = stats.routes_written
            existing_version.notes = notes
        await session.commit()
    print(f"   地点 {stats.places_written} · 别名 {stats.aliases_written} · 路线 {stats.routes_written}")

    report = build_report(stats, kb_version)
    # ``write_report=False`` 是为集成测试准备的：conftest 每次都重建测试库，
    # 于是 ``make check`` 会把 docs/DATA_REPORT.md 一起改写，工作区凭空变脏，
    # 而且报告里的数字来自**测试库**。建库脚本只应在人工调用时落这份文档。
    if write_report:
        report_path = docs_dir() / "DATA_REPORT.md"
        report_path.write_text(report, encoding="utf-8")
        print(f"\n⑥ 数据报告：{report_path}")
    else:
        print("\n⑥ 数据报告：已跳过（write_report=False，调用方为测试）")

    print("\n" + "=" * 70)
    print(report)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="广州知识库建库（幂等）")
    parser.add_argument("--dry-run", action="store_true", help="只分析不写库")
    parser.add_argument("--force", action="store_true", help="即使已有行程引用也重建")
    args = parser.parse_args(argv)
    settings = get_settings()
    print(f"环境：{settings.env} · 数据库：{settings.database_url.rsplit('@', 1)[-1]}")
    return asyncio.run(run(dry_run=args.dry_run, force=args.force))


if __name__ == "__main__":
    sys.exit(main())
