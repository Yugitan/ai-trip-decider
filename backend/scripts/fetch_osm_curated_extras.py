#!/usr/bin/env python3
"""定向补抓：把人工确认过、但全量抓取漏掉的目的地补回来。

背景（实测）：
    按类别全量抓取的过滤器是「tourism / historic / leisure / natural / amenity / shop / railway」，
    于是 `place=*`（街区、岛屿、村落）这类要素**根本没被查询**，
    结果永庆坊、二沙岛、东山口、小洲村这些真实目的地全部缺失。

    另一类漏抓来自命名差异：OSM 里叫「粤海关旧址（中国海关博物馆广州分馆）」，
    而常识里我们叫它「粤海关博物馆」。

做法：用人工清单里的名称做**子串正则**，一次性在 Overpass 里查 node/way/relation。
    这是一次"由人工知识驱动的定向查询"，而不是无差别扩大抓取范围 ——
    既补上了缺口，又不会把几万条无关要素再拉进来。

输出：data/raw/osm_guangzhou_curated_extras.json（与主抓取同结构，seed 会自动合并）

用法：
    python scripts/fetch_osm_curated_extras.py
    python scripts/fetch_osm_curated_extras.py --names 永庆坊,二沙岛
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from app.schemas.curated import load_curated_places
from scripts.fetch_osm_guangzhou import (
    DEFAULT_BBOX,
    normalize_name,
    query_overpass,
    to_record,
)

# place=* 在 OSM 里代表"有人居住/被命名的地理单元"，是街区类目的主要来源。
# 这些元素不在主抓取的类别过滤器里，所以这里额外显式查询一次。
PLACE_FILTERS = ("suburb", "neighbourhood", "quarter", "village", "island", "square", "city_block", "town")


def build_extras_query(names: list[str], bbox: tuple[float, float, float, float]) -> str:
    south, west, north, east = bbox
    area = f"({south},{west},{north},{east})"
    alternation = "|".join(names)
    parts = []
    # 1) 按名称子串匹配（覆盖 OSM 命名与我们常识命名不一致的情况）
    for element_type in ("node", "way", "relation"):
        parts.append(f'  {element_type}["name"~"{alternation}"]{area};')
    # 2) 显式补 place=* 类目（街区/岛屿/村落）
    for element_type in ("node", "way", "relation"):
        for value in PLACE_FILTERS:
            parts.append(f'  {element_type}["name"]["place"="{value}"]{area};')
    body = "\n".join(parts)
    return f"[out:json][timeout:180];\n(\n{body}\n);\nout center 20000;"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="按人工清单定向补抓 OSM 地点")
    parser.add_argument("--out", type=Path, default=Path("data/raw"))
    parser.add_argument("--names", default=None, help="逗号分隔的名称列表（默认取人工清单全部名称与别名）")
    parser.add_argument("--bbox", default=",".join(str(v) for v in DEFAULT_BBOX))
    args = parser.parse_args(argv)

    if args.names:
        names = [n.strip() for n in args.names.split(",") if n.strip()]
    else:
        curated = load_curated_places(Path("data/curated/guangzhou_places.yaml"))
        names = sorted({entry.name for entry in curated.places})
        # 只取名称主体（去掉过长后缀），提高正则命中率
        names = sorted({n for n in names if 2 <= len(n) <= 10})

    south, west, north, east = (float(v) for v in args.bbox.split(","))
    bbox: tuple[float, float, float, float] = (south, west, north, east)
    query = build_extras_query(names, bbox)
    print(f"定向补抓 {len(names)} 个名称，查询 {len(query)} 字节")

    payload, mirror = query_overpass(query, label="curated_extras")
    elements = payload.get("elements", [])
    print(f"  {mirror.split('/')[2]} 返回 {len(elements)} 个元素")

    # None 表示"该键取任意值"（见 fetch_osm_guangzhou.value_filters 的定义）
    any_value: tuple[str, ...] | None = None
    specs: list[tuple[str, tuple[str, ...] | None]] = [
        ("tourism", any_value),
        ("historic", any_value),
        ("leisure", any_value),
        ("natural", any_value),
        ("amenity", any_value),
        ("shop", any_value),
        ("railway", any_value),
        ("place", any_value),
    ]
    records = []
    seen: set[tuple[str, int]] = set()
    for element in elements:
        record = to_record(element, specs)
        if record is None:
            continue
        key = (record["osm_type"], record["osm_id"])
        if key in seen:
            continue
        seen.add(key)
        records.append(record)

    # 只保留与人工清单确实相关的记录，避免 place=* 查询把无关街区一起带进来
    wanted = {normalize_name(n) for n in names}
    relevant = [
        r
        for r in records
        if any(w and (w in normalize_name(r["name"]) or normalize_name(r["name"]) in w) for w in wanted)
    ]
    print(f"  转换后 {len(records)} 条，与清单相关 {len(relevant)} 条")

    args.out.mkdir(parents=True, exist_ok=True)
    out_path = args.out / "osm_guangzhou_curated_extras.json"
    out_path.write_text(
        json.dumps(
            {
                "meta": {
                    "collected_at": datetime.now(UTC).isoformat(),
                    "purpose": "按人工清单定向补抓（主抓取漏掉的 destination）",
                    "osm_snapshot_timestamp": (payload.get("osm3s") or {}).get("timestamp_osm_base"),
                    "queried_names": names,
                    "raw_elements": len(elements),
                    "relevant_records": len(relevant),
                    "source": "OpenStreetMap via Overpass API (ODbL)",
                },
                "records": relevant,
            },
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
    print(f"\n写入 {out_path}")
    matched_names = {normalize_name(r["name"]) for r in relevant}
    hit = [n for n in names if any(normalize_name(n) in m or m in normalize_name(n) for m in matched_names)]
    print(f"清单命中 {len(hit)}/{len(names)}：{'、'.join(hit)}")
    missing = [n for n in names if n not in hit]
    if missing:
        print(f"仍未命中（这些地点 OSM 里没有可核验记录，不会入库也不编造来源）：{'、'.join(missing)}")
    time.sleep(1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
