#!/usr/bin/env python3
"""定向补抓：交通枢纽（地铁站/公交站/轮渡码头），补上 B2 数据缺口。

背景（实测，见 TASKS.md 的 B2 行）：
    主抓取 `fetch_osm_guangzhou.py` 的 ``E_transport`` 分组在整块广州 bbox 上
    长期返回 **HTTP 504**。2026-09-15 复测定位到根因：不是数据量，而是**查询写法** ——
    正则过滤器 ``["railway"~"^(station|subway_entrance)$"]`` 让 Overpass 走一遍
    全量正则匹配，4 条语句并入一个查询时网关 43s 就超时；
    拆成逐值等值过滤器后，**同一 bbox、同样的元素** 直接 HTTP 200。
    根因的修复落在 `fetch_osm_guangzhou.value_filters`（全组通用）。

为什么单独一个文件、单独一个脚本，而不是并回主抓取：
    1. 主抓取一次要跑 5 个分组约 9 分钟，并且会把 `osm_guangzhou_raw.json`
       整体重写（3.8MB 的已跟踪文件）。只补一个类目却重写全部 10279 条记录，
       等于让"数据变了"这件事故意变得无法审阅。
    2. `seed_guangzhou.load_raw_records` 按 ``osm_guangzhou_*.json`` 通配读取，
       所以新增第三个文件不需要改动建库链路（脚本顶部注释里早已写明
       "osm_guangzhou_raw.json (+ transport)"）。
    3. 逐取值单独查询，每个查询**自带一份 5000 条的输出上限**，于是可以断言
       "没有触顶"。一条合并查询触顶时只会静默截断 —— 那正是最该避免的失败模式。

输出：data/raw/osm_guangzhou_transport.json（与主抓取同结构，seed 会自动读入）

用法：
    python scripts/fetch_osm_transport.py
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scripts.fetch_osm_guangzhou import (
    DEFAULT_BBOX,
    GROUPS,
    MAX_ELEMENTS_PER_GROUP,
    SLEEP_BETWEEN_QUERIES_S,
    USER_AGENT,
    build_query,
    query_overpass,
    to_record,
)

#: 要补抓的分组。写死成常量而非参数：这是"补 B2 缺口"这一个明确动作，
#: 放开成任意分组会让人以为它是主抓取的替代品。
GROUP = "E_transport"


def iter_value_specs(
    specs: list[tuple[str, tuple[str, ...] | None]],
) -> list[tuple[str, str | None]]:
    """把分组定义展开成 ``[(键, 取值 | None), …]``，每个取值一条独立查询。

    ``None`` 表示"该键取任意值"（与 ``value_filters`` 的约定一致）。
    """
    out: list[tuple[str, str | None]] = []
    for key, values in specs:
        if values is None:
            out.append((key, None))
        else:
            out.extend((key, value) for value in values)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="定向补抓广州交通枢纽（OSM/Overpass）")
    parser.add_argument("--out", type=Path, default=Path("data/raw"), help="输出目录")
    parser.add_argument("--bbox", default=None, help="south,west,north,east（默认广州全域）")
    args = parser.parse_args(argv)

    specs = GROUPS[GROUP]
    bbox = DEFAULT_BBOX
    if args.bbox:
        parts = [float(p) for p in args.bbox.split(",")]
        if len(parts) != 4:
            parser.error("bbox 需要 4 个数字：south,west,north,east")
        bbox = (parts[0], parts[1], parts[2], parts[3])

    units = iter_value_specs(specs)
    print(f"交通枢纽补抓：{len(units)} 个子查询，bbox={bbox}")

    records: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    counts: dict[str, int] = {}
    snapshot_timestamp: str | None = None
    truncated: list[str] = []

    for index, (key, value) in enumerate(units):
        label = f"{key}={value}" if value is not None else key
        query = build_query([(key, (value,) if value is not None else None)], bbox)
        payload, mirror = query_overpass(query, label=label)
        elements = payload.get("elements", [])
        # 触顶即静默截断：宁可这一次抓取失败，也不要把"少了一半的地铁站"
        # 当成完整数据入库（那会得出"某区没有地铁"这种错误结论）。
        if len(elements) >= MAX_ELEMENTS_PER_GROUP:
            truncated.append(label)
        snapshot_timestamp = (
            (payload.get("osm3s") or {}).get("timestamp_osm_base") or snapshot_timestamp
        )
        counts[label] = len(elements)

        kept = 0
        for element in elements:
            record = to_record(element, specs)
            if record is None:
                continue
            dedupe_key = (record["osm_type"], record["osm_id"])
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            records.append(record)
            kept += 1
        print(f"    {mirror.split('/')[2]} → {len(elements)} 元素，新增 {kept} 条（累计 {len(records)}）")

        if index < len(units) - 1:
            time.sleep(SLEEP_BETWEEN_QUERIES_S)

    if truncated:
        raise SystemExit(
            f"查询触顶（{MAX_ELEMENTS_PER_GROUP} 条）会被静默截断，拒绝写入：{truncated}。"
            "请缩小 bbox 或把该取值再拆分后重抓。"
        )

    records.sort(key=lambda r: (r["primary_tag"], r["name"]))
    args.out.mkdir(parents=True, exist_ok=True)
    out_path = args.out / "osm_guangzhou_transport.json"
    out_path.write_text(
        json.dumps(
            {
                "meta": {
                    "collected_at": datetime.now(UTC).isoformat(),
                    "purpose": "定向补抓交通枢纽（主抓取的 E_transport 分组曾整块 504）",
                    "group": GROUP,
                    "osm_snapshot_timestamp": snapshot_timestamp,
                    "bbox": list(bbox),
                    "element_counts": counts,
                    "record_count": len(records),
                    "source": "OpenStreetMap via Overpass API (ODbL)",
                    "user_agent": USER_AGENT,
                    "license_note": (
                        "数据来自 OpenStreetMap 贡献者，遵循 ODbL 许可；"
                        "每条记录的 source_url 指向其 OSM permalink。"
                    ),
                },
                "records": records,
            },
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )

    print(f"\n写入 {out_path}")
    print(f"有效记录 {len(records)} 条，分类计数：{counts}")
    sample = [f"{r['primary_tag']} {r['name']}" for r in records[:12]]
    print("样例：" + "、".join(sample))
    return 0


if __name__ == "__main__":
    sys.exit(main())
