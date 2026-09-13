#!/usr/bin/env python3
"""抓取城市行政边界，用于精确判定"某个地点是否在这座城市里"。

为什么必须做这件事（真实 bug 记录）：
    整城 POI 抓取用的是**矩形 bbox**。广州的形状不规则，矩形四角切进了深圳、东莞、
    佛山、中山 —— 结果「深圳野生动物园」「东莞博物馆」进了广州知识库，还因为信号多、
    热度分值高，排到了列表最前面。这是"城市数据里混进别的城市"的严重问题：
    用户会在广州一日游里看到深圳的动物园。

    只靠地址标签与名称过滤只能解决大部分（OSM 里 528 条有地址标签，其中明确的邻市
    地点能被剔除），但像「锦绣中华民俗村」（深圳华侨城）这种**既无地址标签、
    名称也不含城市名**的地点是漏网的。唯一可靠的判据是行政边界本身。

做法：
    1. 用 Overpass 取 `rel["name"="广州市"]["boundary"="administrative"]` 的几何
    2. 把 relation 的成员 way 拼接成闭合环（外环 + 内环）
    3. 存成紧凑 JSON，之后建库时做离线点面判定（不再依赖网络）

输出：backend/data/reference/{city_slug}_boundary.json

用法：
    python scripts/fetch_city_boundary.py                     # 广州
    python scripts/fetch_city_boundary.py --city 深圳市 --slug shenzhen
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.core.paths import data_dir
from scripts.fetch_osm_guangzhou import query_overpass

COORD_PRECISION = 7

CITY_NAMES = {
    "guangzhou": "广州市",
    "shenzhen": "深圳市",
    "dongguan": "东莞市",
    "foshan": "佛山市",
}


@dataclass(frozen=True)
class Ring:
    """一个闭合环（首尾点相同）。"""

    points: list[tuple[float, float]]
    role: str  # outer | inner

    @property
    def is_closed(self) -> bool:
        return len(self.points) >= 4 and self.points[0] == self.points[-1]


def _key(lat: float, lng: float) -> tuple[float, float]:
    return (round(lat, COORD_PRECISION), round(lng, COORD_PRECISION))


def build_boundary_query(city_name: str) -> str:
    return (
        "[out:json][timeout:180];\n"
        f'rel["name"="{city_name}"]["boundary"="administrative"];\n'
        "out geom;"
    )


def stitch_rings(ways: list[tuple[str, list[tuple[float, float]]]]) -> tuple[list[Ring], int]:
    """把散落的 way 拼成闭合环。

    OSM 的多边形关系由若干条 way 组成，顺序不保证、方向也可能相反，
    因此需要按端点衔接把它们串起来。拼不上的（数据不完整）会被丢弃并计数 ——
    宁可少判，也不要拿一个破洞的多边形去判定归属。
    """
    pending: list[tuple[str, list[tuple[float, float]]]] = [
        (role, [_key(lat, lng) for lat, lng in geom]) for role, geom in ways if len(geom) >= 2
    ]
    rings: list[Ring] = []
    unclosed = 0

    while pending:
        role, current = pending.pop(0)
        changed = True
        while changed and current[0] != current[-1]:
            changed = False
            for index, (other_role, other) in enumerate(pending):
                if other_role != role:
                    continue
                if other[0] == current[-1]:
                    current.extend(other[1:])
                elif other[-1] == current[-1]:
                    current.extend(list(reversed(other))[1:])
                elif other[0] == current[0]:
                    current = list(reversed(other))[:-1] + current
                elif other[-1] == current[0]:
                    current = other[:-1] + current
                else:
                    continue
                pending.pop(index)
                changed = True
                break
        ring = Ring(points=current, role=role)
        if ring.is_closed:
            rings.append(ring)
        else:
            # 拼不成闭合环的残片必须报出来，而不是悄悄丢掉
            unclosed += 1

    return rings, unclosed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="抓取城市行政边界（用于点面判定）")
    parser.add_argument("--slug", default="guangzhou")
    parser.add_argument("--city", default=None, help="OSM 里的城市名，默认按 slug 推导")
    args = parser.parse_args(argv)

    city_name = args.city or CITY_NAMES.get(args.slug)
    if city_name is None:
        parser.error(f"未知 slug {args.slug}，请用 --city 显式指定 OSM 城市名")

    print(f"抓取 {city_name} 的行政边界…")
    payload, mirror = query_overpass(build_boundary_query(city_name), label="city_boundary")
    elements = payload.get("elements", [])
    if not elements:
        raise SystemExit(f"未找到 {city_name} 的行政边界，无法做点面判定")

    relation = elements[0]
    members = relation.get("members", [])
    ways: list[tuple[str, list[tuple[float, float]]]] = []
    for member in members:
        geometry = member.get("geometry")
        if member.get("type") != "way" or not geometry:
            continue
        role = member.get("role") or "outer"
        if role not in ("outer", "inner"):
            role = "outer"
        ways.append((role, [(float(point["lat"]), float(point["lon"])) for point in geometry]))

    print(f"  {mirror.split('/')[2]} 返回 relation {relation['id']}（{relation.get('tags', {}).get('admin_level')} 级），"
          f"{len(ways)} 条 way")
    rings, unclosed = stitch_rings(ways)
    outer = [ring for ring in rings if ring.role == "outer"]
    inner = [ring for ring in rings if ring.role == "inner"]
    print(
        f"  拼接完成：外环 {len(outer)} 个、内环 {len(inner)} 个"
        f"（由 {len(ways)} 条 way 拼成；未闭合残片 {unclosed} 组）"
    )

    if not outer:
        raise SystemExit("没有拼出任何外环，边界数据不完整，拒绝写入（宁可失败也不用残缺边界）")

    out_dir = Path(data_dir()) / "reference"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{args.slug}_boundary.json"
    out_path.write_text(
        json.dumps(
            {
                "meta": {
                    "slug": args.slug,
                    "osm_relation_id": relation["id"],
                    "osm_name": city_name,
                    "admin_level": relation.get("tags", {}).get("admin_level"),
                    "fetched_at": datetime.now(UTC).isoformat(),
                    "source": f"https://www.openstreetmap.org/relation/{relation['id']}",
                    "license": "ODbL (OpenStreetMap contributors)",
                    "note": "点面判定用；外环取并集、内环取差集（洞）",
                },
                "outer": [[list(point) for point in ring.points] for ring in outer],
                "inner": [[list(point) for point in ring.points] for ring in inner],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    print(f"\n写入 {out_path}（{out_path.stat().st_size / 1024:.0f} KB）")
    print(f"外环点数：{[len(ring.points) for ring in outer]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
