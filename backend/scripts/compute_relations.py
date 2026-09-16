#!/usr/bin/env python3
"""地点关系图计算（place_relations）。

为什么需要关系图：
    候选生成与路线组合要回答"这两个地点连着走是否合理、要多久"。如果每次规划都去
    问地图服务，成本会失控（PRD §15.1 要求 L4「地点关系数据」为免费层）。
    所以关系图是**预计算 + 长缓存（30 天）**的资产。

★ 关于"距离与时间"的诚实性（实测结论，见 app/domain/geo.py）★
    公共 OSRM 演示实例只部署了 car profile：请求 `/route/v1/foot/...` 与
    `/route/v1/driving/...` 返回**完全相同**的结果（实测 2349m / 189.6s → 44.6 km/h）。
    因此：
      - 默认（离线）模式：距离用 haversine × 绕行系数，全部标 `estimated`
      - `--osrm` 模式：距离与**行车**耗时取自 OSRM（真实路网），
        步行/公交/骑行耗时由**真实距离 × config/limits.yaml 的速度假设**推导，
        并把这些方式记入 `derived_modes` 列 —— 绝不让推导值冒充实测值。

范围控制（避免 2000 个地点两两组合产生几百万条无意义关系）：
    只为"可能同一天连在一起"的地点对建关系：
      - 距离 ≤ limits.relation_graph.max_pair_distance_m（默认 6km）
      - 每个地点只与其最近的 K 个邻居建关系（K 默认 12）
      - 地点集合默认限制为"路线站点 ∪ 高热度地点"，上限 --max-places（默认 800）

用法：
    python scripts/compute_relations.py                      # 离线估算（快，无网络）
    python scripts/compute_relations.py --osrm                # 用真实路网距离（需网络）
    python scripts/compute_relations.py --max-places 400 --neighbors 8
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import uuid
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import delete, select, text

from app.core.config import get_limits_config, get_settings, get_ttl_config
from app.core.paths import docs_dir
from app.db.models import City, Place, PlaceRelation
from app.db.session import get_sessionmaker
from app.domain.geo import haversine_m, route_factor, travel_minutes

OSRM_TABLE_URL = "https://router.project-osrm.org/table/v1/driving/{coords}"

# 互补性：不同类别的组合价值（PRD §11.4）
_COMPLEMENTARITY: dict[tuple[str, str], float] = {
    ("food", "attraction"): 0.90,
    ("food", "historic"): 0.88,
    ("food", "museum"): 0.85,
    ("food", "district"): 0.90,
    ("food", "nightview"): 0.85,
    ("cafe", "citywalk"): 0.88,
    ("cafe", "district"): 0.85,
    ("cafe", "photo"): 0.82,
    ("nightview", "district"): 0.90,
    ("nightview", "nature"): 0.78,
    ("museum", "historic"): 0.80,
    ("nature", "family"): 0.82,
    ("shopping", "food"): 0.80,
}
# 同类组合仍然合理（同一片区连着逛两家咖啡馆也常见），但不如互补组合
_SAME_CATEGORY_SCORE = 0.45
_DEFAULT_COMPLEMENTARITY = 0.62


@dataclass
class PairMetrics:
    distance_m: int
    walking_time_min: int
    driving_time_min: int | None
    transit_time_min: int
    cycling_time_min: int
    data_source: str
    derived_modes: tuple[str, ...]


def complementarity(cat_a: str, cat_b: str) -> float:
    if cat_a == cat_b:
        return _SAME_CATEGORY_SCORE
    return _COMPLEMENTARITY.get((cat_a, cat_b)) or _COMPLEMENTARITY.get((cat_b, cat_a)) or _DEFAULT_COMPLEMENTARITY


def proximity_score(distance_m: float) -> float:
    """距离衰减：≤600m → 1.0；3000m → 0.5；≥6000m → 0.1（与 PRD §11.4 一致）。"""
    if distance_m <= 600:
        return 1.0
    if distance_m >= 6000:
        return 0.1
    # 600→3000 线性降到 0.5；3000→6000 线性降到 0.1
    if distance_m <= 3000:
        return 1.0 - 0.5 * (distance_m - 600) / 2400
    return 0.5 - 0.4 * (distance_m - 3000) / 3000


def transit_quality(transit_min: int) -> float:
    """公共交通可达性：≤20min → 1.0；≥60min → 0.1。"""
    if transit_min <= 20:
        return 1.0
    if transit_min >= 60:
        return 0.1
    return 1.0 - 0.9 * (transit_min - 20) / 40


def compute_pair(
    a: Place,
    b: Place,
    *,
    osrm_distance_m: float | None,
    limits: object,
) -> PairMetrics:
    """计算一对地点的距离与各出行方式耗时。

    ``osrm_distance_m`` 为 None 时全部走估算；否则距离与行车耗时是真实路网数据。
    """
    modes = limits.travel_modes  # type: ignore[attr-defined]
    crow = haversine_m(a.latitude, a.longitude, b.latitude, b.longitude)
    if osrm_distance_m is not None:
        distance = osrm_distance_m
        data_source = "osrm"
        driving_min = travel_minutes(distance, modes["taxi"].speed_kmh, overhead_min=modes["taxi"].overhead_min)
    else:
        distance = crow * route_factor(crow)
        data_source = "estimated"
        driving_min = travel_minutes(distance, modes["taxi"].speed_kmh, overhead_min=modes["taxi"].overhead_min)

    # 步行/公交/骑行：**永远是推导值**（公共 OSRM 没有这些 profile），必须如实标注
    walking = travel_minutes(distance, modes["walk"].speed_kmh, overhead_min=modes["walk"].overhead_min)
    transit = travel_minutes(distance, modes["metro"].speed_kmh, overhead_min=modes["metro"].overhead_min)
    cycling = travel_minutes(distance, modes["bike"].speed_kmh, overhead_min=modes["bike"].overhead_min)

    return PairMetrics(
        distance_m=round(distance),
        walking_time_min=walking,
        driving_time_min=driving_min,
        transit_time_min=transit,
        cycling_time_min=cycling,
        data_source=data_source,
        derived_modes=("walk", "metro", "bike"),
    )


def count_eligible_pairs(
    pairs: Iterable[tuple[uuid.UUID, uuid.UUID]], chunk_of: Mapping[uuid.UUID, int]
) -> int:
    """有多少候选对**有可能**拿到真实路网距离。

    OSRM 的 table 接口一次只算**一批点内部**的矩阵（`batch_size`，默认 25），
    所以一对地点只有落在同一批里才可能拿到真实距离 —— 跳批次的必然退回估算。
    这个数就是 `osrm_pairs` 的**结构性上限**，而不是"网络好不好"的指标。

    为什么必须单独记它：早期报告里只有 `osrm_pairs`，读者无法分辨"这对是跳批次"
    还是"OSRM 挂了/失败了" —— 实测一次重算从 163 变成 161（地点集合变了，
    于是按 UUID 排序后的分批边界也变了），而报告里 **看不出** 是哪种原因。
    """
    eligible = 0
    for a, b in pairs:
        index = chunk_of.get(a)
        # 两个都没登记（`.get` 都返回 None）不算“同批”：没分过批的点对不可能被覆盖。
        if index is not None and index == chunk_of.get(b):
            eligible += 1
    return eligible


def recommend_transport(distance_m: float) -> str:
    if distance_m <= 1500:
        return "walk"
    if distance_m <= 8000:
        return "metro"
    return "taxi"


async def fetch_osrm_distances(
    client: httpx.AsyncClient, coords: list[tuple[float, float]]
) -> dict[tuple[int, int], float]:
    """用 OSRM table 服务一次取回 n×n 距离矩阵（n ≤ 100）。"""
    coord_str = ";".join(f"{lng:.6f},{lat:.6f}" for lat, lng in coords)
    url = OSRM_TABLE_URL.format(coords=coord_str)
    response = await client.get(url, params={"annotations": "distance"})
    response.raise_for_status()
    payload = response.json()
    if payload.get("code") != "Ok":
        raise RuntimeError(f"OSRM 返回错误：{payload.get('code')}")
    matrix = payload.get("distances") or []
    result: dict[tuple[int, int], float] = {}
    for i, row in enumerate(matrix):
        for j, value in enumerate(row):
            if i != j and value is not None:
                result[(i, j)] = float(value)
    return result


async def build_relations(*, use_osrm: bool, max_places: int, neighbors: int) -> dict[str, object]:
    limits = get_limits_config()
    settings = get_settings()
    graph_limits = limits.relation_graph
    max_pair_distance = graph_limits.max_pair_distance_m

    maker = get_sessionmaker()
    # 报告必须能自己说明"这是哪种模式、什么参数"：同一份文件既可能是离线估算
    # 也可能是真实路网，不写清楚就只能靠猜（而且一次离线重算会静静地把上一次
    # 带 OSRM 数字的报告覆盖掉，文件里不留任何痕迹）。
    stats: dict[str, object] = {
        "use_osrm": use_osrm,
        "max_places": max_places,
        "neighbors": neighbors,
    }

    async with maker() as session:
        city = (await session.execute(select(City).where(City.slug == "guangzhou"))).scalar_one_or_none()
        if city is None:
            raise SystemExit("数据库里还没有广州，请先运行 make seed")

        # 只取"值得建关系"的地点：路线站点优先，其次高热度地点
        route_place_ids: set[uuid.UUID] = {
            row[0]
            for row in (
                await session.execute(
                    text(
                        """
                        SELECT DISTINCT rp.place_id FROM route_places rp
                        JOIN routes r ON r.id = rp.route_id WHERE r.city_id = :c
                        """
                    ),
                    {"c": city.id},
                )
            ).all()
        }
        fetched: list[Place] = list(
            (
                await session.execute(
                    select(Place)
                    .where(Place.city_id == city.id, Place.status == "active")
                    .order_by(Place.popularity_score.desc().nulls_last(), Place.canonical_name)
                    .limit(max_places * 3)
                )
            )
            .scalars()
            .all()
        )

        # 排序：路线站点排最前，其余按热度（保证模板路线的站点一定建上关系）
        fetched.sort(key=lambda place: (0 if place.id in route_place_ids else 1, -(place.popularity_score or 0)))
        places = fetched[:max_places]
        by_id: dict[uuid.UUID, Place] = {place.id: place for place in places}
        stats["selected_places"] = len(places)
        stats["route_places_included"] = sum(1 for place in places if place.id in route_place_ids)

        # 用网格加速近邻查找（0.06° ≈ 6.6km，保证 6km 内邻居落在 3×3 网格内）
        cell = 0.06
        grid: dict[tuple[int, int], list[Place]] = defaultdict(list)
        for place in places:
            grid[(int(place.latitude / cell), int(place.longitude / cell))].append(place)

        pairs: set[tuple[uuid.UUID, uuid.UUID]] = set()
        for place in places:
            cx, cy = int(place.latitude / cell), int(place.longitude / cell)
            near: list[tuple[float, Place]] = []
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for other in grid.get((cx + dx, cy + dy), []):
                        if other.id == place.id:
                            continue
                        distance = haversine_m(
                            place.latitude, place.longitude, other.latitude, other.longitude
                        )
                        if distance <= max_pair_distance:
                            near.append((distance, other))
            near.sort(key=lambda item: item[0])
            for _distance, other in near[:neighbors]:
                low, high = sorted((place.id, other.id))
                pairs.add((low, high))
        stats["candidate_pairs"] = len(pairs)
        if not pairs:
            return stats | {"written": 0}

        # ── 可选：用 OSRM 取真实路网距离（只对候选对做，避免几万次调用）──
        osrm_matrix: dict[tuple[uuid.UUID, uuid.UUID], float] = {}
        stats["batch_size"] = graph_limits.batch_size
        if use_osrm:
            batch = graph_limits.batch_size
            unique_ids: list[uuid.UUID] = sorted({pid for pair in pairs for pid in pair})
            chunk_of: dict[uuid.UUID, int] = {}
            batches_ok = 0
            batches_failed = 0
            transport = httpx.AsyncClient(
                timeout=max(20.0, limits.providers.map_timeout_s * 3),
                headers={"User-Agent": settings.nominatim_user_agent},
            )
            async with transport as client:
                for start in range(0, len(unique_ids), batch):
                    chunk = unique_ids[start : start + batch]
                    # 先登记"这个点在第几批"，失败也不例外 —— 否则那一批的点会
                    # 被当成"没分过批"，报告里的结构性上限就少算了。
                    for pid in chunk:
                        chunk_of[pid] = start // batch
                    coords = [(by_id[pid].latitude, by_id[pid].longitude) for pid in chunk]
                    try:
                        # fetch_osrm_distances 返回的是"矩阵下标对 → 米"，
                        # 这里立刻换回真实 place_id，避免下标与 UUID 混用（曾因此写错键）。
                        raw_matrix = await fetch_osrm_distances(client, coords)
                        batches_ok += 1
                    except Exception as exc:  # 单个批次失败不影响整体，该批退回估算
                        batches_failed += 1
                        print(f"    ⚠︎ OSRM 批次 {start // batch + 1} 失败（{type(exc).__name__}），该批退回估算")
                        continue
                    for (row, col), meters in raw_matrix.items():
                        low, high = sorted((chunk[row], chunk[col]))
                        osrm_matrix[(low, high)] = meters
                    await asyncio.sleep(0.4)  # 对公共实例保持礼貌
            # 注意：osrm_matrix 里可能含"同批次内但非候选对"的格子（table 是一次算整块矩阵），
            # 所以不能用 len(osrm_matrix) 去减 len(pairs) —— 早期实现这么算，
            # 结果打印出 -1155 这种负数，明显是错的统计口径。
            osrm_pair_count = sum(1 for pair in pairs if pair in osrm_matrix)
            eligible = count_eligible_pairs(pairs, chunk_of)
            stats["osrm_pairs"] = osrm_pair_count
            stats["estimated_pairs"] = len(pairs) - osrm_pair_count
            stats["osrm_eligible_pairs"] = eligible
            stats["osrm_batches_ok"] = batches_ok
            stats["osrm_batches_failed"] = batches_failed
            # 把"结构性上限"与"实际拿到"一并写进报告：只有这两个数都在，
            # 读者才能分辨 osrm_pairs 的涨跌是分批边界变了还是服务出错了。
            gap_note = (
                "差额来自 OSRM 判为不可路由的点对"
                if batches_failed == 0
                else "差额里既有失败批次、也有 OSRM 判为不可路由的点对"
            )
            stats["osrm_note"] = (
                f"OSRM 的 table 接口一次只算一批（batch_size={batch}）内部的矩阵，"
                f"所以只有同批的 {eligible} 对**可能**拿到真实路网距离；实际拿到 {osrm_pair_count} 对（{gap_note}），"
                f"其余 {len(pairs) - eligible} 对跳批次，按设计退回估算。"
                f"这与“公共 OSRM 可以用多久”无关：coverage 的上限就是分批结构。"
            )

        # ── 写库 ──
        await session.execute(delete(PlaceRelation).where(PlaceRelation.city_id == city.id))
        # 关系图 TTL 来自 config/ttl.yaml（place_relation，默认 30 天）：
        # 道路与站点不会天天变，长缓存正是 L4「地点关系数据」免费层的基础。
        relation_ttl_hours = get_ttl_config().ttl_for("place_relation") or 720
        expires = datetime.now(UTC) + timedelta(hours=relation_ttl_hours)

        written = 0
        source_counts: dict[str, int] = {}
        for low_id, high_id in sorted(pairs):
            place_a, place_b = by_id[low_id], by_id[high_id]
            metrics = compute_pair(
                place_a, place_b, osrm_distance_m=osrm_matrix.get((low_id, high_id)), limits=limits
            )
            relation_score = round(
                0.45 * proximity_score(metrics.distance_m)
                + 0.25 * complementarity(place_a.category, place_b.category)
                # cooccurrence：模板路线共现频率，当前用中性值占位，待 M6 用真实统计替换
                + 0.20 * 0.5
                + 0.10 * transit_quality(metrics.transit_time_min),
                3,
            )
            source_label = "真实路网" if metrics.data_source == "osrm" else "直线估算"
            session.add(
                PlaceRelation(
                    city_id=city.id,
                    place_a_id=low_id,
                    place_b_id=high_id,
                    distance_m=metrics.distance_m,
                    walking_time_min=metrics.walking_time_min,
                    driving_time_min=metrics.driving_time_min,
                    transit_time_min=metrics.transit_time_min,
                    cycling_time_min=metrics.cycling_time_min,
                    relationship_score=relation_score,
                    recommended_transport=recommend_transport(metrics.distance_m),
                    recommended_together=metrics.distance_m <= 4000,
                    reason=(
                        f"{source_label}距离 {metrics.distance_m / 1000:.1f}km；"
                        f"步行约 {metrics.walking_time_min} 分钟（由距离推导，非实测）"
                    ),
                    data_source=metrics.data_source,
                    derived_modes=list(metrics.derived_modes),
                    expires_at=expires,
                )
            )
            written += 1
            source_counts[metrics.data_source] = source_counts.get(metrics.data_source, 0) + 1
            if written % 1000 == 0:
                await session.flush()
        await session.commit()
        stats["written"] = written
        stats["data_source_counts"] = source_counts

    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="计算地点关系图")
    parser.add_argument("--osrm", action="store_true", help="用真实路网距离（需联网，较慢）")
    parser.add_argument("--max-places", type=int, default=800)
    parser.add_argument("--neighbors", type=int, default=12)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    started = time.monotonic()
    print(f"计算地点关系图（{'真实路网 OSRM' if args.osrm else '离线估算'}）…")
    stats = asyncio.run(
        build_relations(use_osrm=args.osrm, max_places=args.max_places, neighbors=args.neighbors)
    )
    stats["generated_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    stats["elapsed_s"] = round(time.monotonic() - started, 1)

    output = json.dumps(stats, ensure_ascii=False, indent=2)
    print(output)
    (docs_dir() / "RELATION_REPORT.json").write_text(output, encoding="utf-8")
    print(f"\n报告：{docs_dir() / 'RELATION_REPORT.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
