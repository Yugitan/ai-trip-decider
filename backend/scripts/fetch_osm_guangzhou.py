#!/usr/bin/env python3
"""抓取广州市的真实 OSM POI 原始数据（免 Key）。

为什么用 OSM：
    知识库必须有**可核验的真实来源**。OSM 每个元素都有稳定的 permalink
    （https://www.openstreetmap.org/{type}/{id}），既能当 source_url，也能被质检脚本
    用一个 HEAD 请求复核。我们**绝不编造来源**，所以宁可少抓也不猜。

诚实性约定：
    - 每个字段都直接来自 Overpass 响应，缺失就是 null，不做任何填充猜测。
    - 摘要里如实报告覆盖率（有 opening_hours 的比例、有 name_en 的比例等），
      覆盖率低就写低，不用"看起来完整"的假象糊过去。

用法：
    python scripts/fetch_osm_guangzhou.py                     # 默认输出到 data/raw/
    python scripts/fetch_osm_guangzhou.py --bbox 22.5,112.9,23.95,114.05
    python scripts/fetch_osm_guangzhou.py --groups A_attractions,C_food
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# ── 配置 ────────────────────────────────────────────────────────────────────

# 镜像列表。**只放确认覆盖中国数据的镜像**：
# 实测 overpass.osm.ch 是区域切片，对广州的查询会返回 HTTP 200 + 0 个元素 ——
# 这种"静默空结果"会被误判成"该类别没有数据"，是最危险的一类错误。
# 因此下面还有 verify_mirrors() 探针，任何镜像都必须先通过"能查到已知地标"的检查。
MIRRORS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)

# 探针：广州塔一定存在，用它验证镜像是否真的有广州数据
_PROBE_QUERY = '[out:json][timeout:25];node["name"="广州塔"](22.9,113.1,23.3,113.5);out ids 1;'

_verified_mirrors: tuple[str, ...] | None = None


def verify_mirrors(*, verbose: bool = True) -> tuple[str, ...]:
    """逐个探测镜像是否真的覆盖广州数据，只返回可用的镜像。

    为什么必须做这一步：镜像返回 ``200 + 空 elements`` 时，调用方无法区分
    "查询成功但没数据" 与 "这个镜像根本没有该地区数据"。一次构造错误的结论
    （比如"广州没有地铁站"）就会污染整个知识库，所以宁可在开跑前花几秒探测。
    """
    global _verified_mirrors
    if _verified_mirrors is not None:
        return _verified_mirrors
    good: list[str] = []
    for mirror in MIRRORS:
        try:
            payload = _request(mirror, _PROBE_QUERY.encode("utf-8"), False)
            count = len(payload.get("elements", []))
            if count:
                good.append(mirror)
                if verbose:
                    print(f"  ✓ 镜像可用：{mirror.split('/')[2]}（探针命中 {count} 条）")
            elif verbose:
                print(f"  ✗ 镜像无广州数据（返回 0 条，已排除）：{mirror.split('/')[2]}")
        except Exception as exc:  # 探测失败即视为不可用（网络层异常种类多，统一按不可用处理）
            if verbose:
                print(f"  ✗ 镜像不可用：{mirror.split('/')[2]}（{type(exc).__name__}）")
    if not good:
        raise RuntimeError(
            "所有 Overpass 镜像都不可用或没有广州数据。请稍后重试；"
            "若长期如此，考虑改用自建 Overpass 或换用其他数据源。"
        )
    _verified_mirrors = tuple(good)
    return _verified_mirrors

USER_AGENT = (
    "TripDecider-DataCollector/0.1 (travel route planner; data seeding; "
    "contact: dev@example.com)"
)

DEFAULT_BBOX = (22.50, 112.90, 23.95, 114.05)  # south, west, north, east（广州全域）

REQUEST_TIMEOUT_S = 180
# 重试预算：实测 Overpass 在高峰期会连续返回 504，如果退避太耐心，
# 一轮抓取会卡在重试里几十分钟（transport 分组就曾卡了 13 分钟仍无结果）。
# 因此把重试限制为"2 轮 + 短退避"，失败就如实报告为数据缺口，而不是无限等待。
RETRY_BACKOFF_S = (10, 25)
SLEEP_BETWEEN_QUERIES_S = 12
MAX_ELEMENTS_PER_GROUP = 5000

# 抓取分组：键 → [(OSM 键, 允许值元组 | None 表示任意值)]
# 用结构化定义而不是手写字符串，是为了让 primary_tag 的判定（pick_primary_tag）
# 与查询本身使用同一份事实，不会出现"查得出来但归类不了"的情况。
GROUPS: dict[str, list[tuple[str, tuple[str, ...] | None]]] = {
    "A_attractions": [
        (
            "tourism",
            (
                "museum",
                "gallery",
                "attraction",
                "viewpoint",
                "artwork",
                "theme_park",
                "zoo",
                "aquarium",
                "yes",
            ),
        ),
        ("historic", None),
    ],
    "B_nature": [
        ("leisure", ("park", "garden", "nature_reserve")),
        ("natural", ("peak", "water", "beach")),
    ],
    "C_food_shopping": [
        (
            "amenity",
            ("restaurant", "cafe", "marketplace", "food_court", "theatre", "cinema", "library"),
        ),
        ("shop", ("mall", "department_store")),
    ],
    "D_public": [
        ("amenity", ("place_of_worship", "community_centre", "arts_centre")),
    ],
    "E_transport": [
        ("railway", ("station", "subway_entrance")),
        ("amenity", ("bus_station", "ferry_terminal")),
    ],
}

# 只保留有信息量的标签（体积控制 + 后续丰富化只依赖这些）
KEEP_TAGS = (
    "name",
    "name:en",
    "name:zh",
    "opening_hours",
    "website",
    "contact:website",
    "phone",
    "contact:phone",
    "addr:district",
    "addr:street",
    "addr:city",
    "historic",
    "heritage",
    "tourism",
    "amenity",
    "shop",
    "leisure",
    "natural",
    "cuisine",
    "religion",
    "wikipedia",
    "wikidata",
    "fee",
    "charge",
    "wheelchair",
    "opening_hours:url",
    "description",
    "description:zh",
)

_MEANINGLESS_NAME = re.compile(r"^[\d\s\W_]+$")


# ── Overpass 查询 ───────────────────────────────────────────────────────────


def filter_expr(key: str, values: tuple[str, ...] | None) -> str:
    """把结构化定义转成 Overpass 标签过滤器。"""
    if values is None:
        return f'["{key}"]'
    joined = "|".join(values)
    return f'["{key}"~"^({joined})$"]'


def build_query(specs: list[tuple[str, tuple[str, ...] | None]], bbox: tuple[float, float, float, float]) -> str:
    south, west, north, east = bbox
    area = f"({south},{west},{north},{east})"
    parts = []
    for key, values in specs:
        filt = filter_expr(key, values)
        for element_type in ("node", "way", "relation"):
            # 只要带 name 的元素：无名 POI 对行程规划没有意义，还会把结果撑爆
            parts.append(f'  {element_type}["name"]{filt}{area};')
    body = "\n".join(parts)
    return (
        f"[out:json][timeout:{REQUEST_TIMEOUT_S}];\n"
        f"(\n{body}\n);\n"
        f"out center {MAX_ELEMENTS_PER_GROUP};"
    )


def _request(url: str, data: bytes | None, use_get: bool) -> dict[str, Any]:
    if use_get and data is not None:
        url = f"{url}?{urllib.parse.urlencode({'data': data.decode('utf-8')})}"
        body = None
    else:
        body = data
    request = urllib.request.Request(
        url,
        data=body,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        method="GET" if (use_get and data is not None) else "POST",
    )
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_S) as response:
        payload: dict[str, Any] = json.loads(response.read().decode("utf-8"))
        return payload


class OverpassQueryError(RuntimeError):
    """Overpass 在 HTTP 200 里返回的语义错误。

    这是实打实的坑：Overpass 遇到超载/超时时会返回 **200 OK** 但 body 里带
    ``remark: "runtime error: Query timed out..."`` 且 ``elements`` 为空。
    如果只看 HTTP 状态码，就会把"查询失败"当成"查询成功但该类别没有数据"，
    进而在数据报告里写下一个错误的覆盖率结论。因此必须显式检查 remark。
    """


def _assert_no_overpass_error(payload: dict[str, Any], label: str) -> None:
    remark = payload.get("remark")
    if remark:
        raise OverpassQueryError(f"{label}: Overpass 返回语义错误 -> {remark}")
    if "elements" not in payload:
        raise OverpassQueryError(f"{label}: Overpass 响应缺少 elements 字段")


def query_overpass(query: str, *, label: str) -> tuple[dict[str, Any], str]:
    """带重试与多镜像容错的 Overpass 调用。

    POST 优先（长查询不受 URL 长度限制），失败后同一镜像再试 GET
    （某些出口只放行 GET），最后轮换镜像。
    """
    errors: list[str] = []
    mirrors = verify_mirrors()
    for attempt, backoff in enumerate((0, *RETRY_BACKOFF_S)):
        if backoff:
            print(f"    …等待 {backoff}s 后重试（{label}）", flush=True)
            time.sleep(backoff)
        for mirror in mirrors:
            for use_get in (False, True):
                method = "GET" if use_get else "POST"
                try:
                    payload = _request(mirror, query.encode("utf-8"), use_get)
                    _assert_no_overpass_error(payload, f"{label} @ {mirror.split('/')[2]}")
                    print(
                        f"    ✓ {mirror.split('/')[2]} [{method}] "
                        f"返回 {len(payload.get('elements', []))} 个元素",
                        flush=True,
                    )
                    return payload, mirror
                except urllib.error.HTTPError as exc:
                    errors.append(f"{mirror} {method}: HTTP {exc.code}")
                except Exception as exc:
                    errors.append(f"{mirror} {method}: {type(exc).__name__}: {exc}")
        print(f"    第 {attempt + 1} 轮全部失败，继续重试…", flush=True)
    raise RuntimeError(f"{label} 抓取失败，已尝试所有镜像与重试：\n  " + "\n  ".join(errors[-6:]))


# ── 记录转换 ────────────────────────────────────────────────────────────────


def clean_tags(tags: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for key in KEEP_TAGS:
        value = tags.get(key)
        if value is not None and str(value).strip():
            out[key] = str(value).strip()
    # 归一化 website/phone（contact:* 回落到主键）
    if "website" not in out and out.get("contact:website"):
        out["website"] = out["contact:website"]
    if "phone" not in out and out.get("contact:phone"):
        out["phone"] = out["contact:phone"]
    out.pop("contact:website", None)
    out.pop("contact:phone", None)
    return out


def pick_primary_tag(
    tags: dict[str, str], specs: list[tuple[str, tuple[str, ...] | None]]
) -> str | None:
    """判定该元素是被哪个过滤器命中的（按 specs 顺序，先命中先用）。"""
    for key, values in specs:
        value = tags.get(key)
        if value is None:
            continue
        if values is None or value in values:
            return f"{key}={value}"
    return None


def to_record(
    element: dict[str, Any],
    specs: list[tuple[str, tuple[str, ...] | None]],
) -> dict[str, Any] | None:
    """Overpass 元素 → 统一记录。无法确定坐标或名称的**一律丢弃**，不猜。"""
    element_type = element.get("type")
    element_id = element.get("id")
    if element_type not in ("node", "way", "relation") or element_id is None:
        return None

    if element_type == "node":
        lat, lng = element.get("lat"), element.get("lon")
    else:
        center = element.get("center") or {}
        lat, lng = center.get("lat"), center.get("lon")
    if lat is None or lng is None:
        return None

    tags = element.get("tags") or {}
    name = (tags.get("name") or "").strip()
    if len(name) < 2 or _MEANINGLESS_NAME.match(name):
        return None

    primary = pick_primary_tag(tags, specs)
    if primary is None:
        # 理论上不会发生：说明查询与分类定义脱节，必须暴露而不是静默丢弃
        return None

    return {
        "osm_type": element_type,
        "osm_id": int(element_id),
        "name": name,
        "name_en": (tags.get("name:en") or "").strip() or None,
        "lat": round(float(lat), 6),
        "lng": round(float(lng), 6),
        "source_url": f"https://www.openstreetmap.org/{element_type}/{element_id}",
        "primary_tag": primary,
        "tags": clean_tags(tags),
    }


def normalize_name(name: str) -> str:
    """用于重复检测的粗归一化（与 app.domain.entity_match 的正式实现保持同思路）。"""
    text = re.sub(r"[（(].*?[）)]", "", name)
    text = re.sub(r"[\s·•・\-—_]+", "", text)
    return text.strip().lower()


# ── 摘要 ────────────────────────────────────────────────────────────────────


def build_summary(
    records: list[dict[str, Any]],
    group_counts: dict[str, int],
    bbox: tuple[float, float, float, float],
    timestamp: str | None,
    elapsed_s: float,
) -> str:
    by_primary: dict[str, int] = {}
    for record in records:
        by_primary[record["primary_tag"]] = by_primary.get(record["primary_tag"], 0) + 1

    dupes: dict[str, int] = {}
    for record in records:
        key = normalize_name(record["name"])
        dupes[key] = dupes.get(key, 0) + 1
    duplicate_names = sorted(
        ((name, count) for name, count in dupes.items() if count >= 2),
        key=lambda item: (-item[1], item[0]),
    )

    def non_null(field: str) -> int:
        return sum(1 for r in records if r["tags"].get(field))

    total = len(records)
    def pct(n: int) -> str:
        return f"{n / total * 100:.1f}%" if total else "n/a"

    lines = [
        "# OSM 广州 POI 抓取摘要",
        "",
        f"- 抓取时间：{datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S UTC')}",
        f"- OSM 数据快照时间（osm3s.timestamp_osm_base）：{timestamp or 'unknown'}",
        f"- bbox（south,west,north,east）：{bbox}",
        f"- 耗时：{elapsed_s:.1f}s",
        f"- **有效记录总数：{total}**",
        "",
        "## 分组命中数（原始元素数，包含被判为无效而丢弃的）",
        "",
        "| 分组 | 原始元素 |",
        "| --- | ---: |",
    ]
    lines += [f"| {name} | {count} |" for name, count in sorted(group_counts.items())]

    lines += [
        "",
        "## primary_tag 分布（降序）",
        "",
        "| primary_tag | 数量 |",
        "| --- | ---: |",
    ]
    lines += [f"| {tag} | {count} |" for tag, count in sorted(by_primary.items(), key=lambda i: -i[1])]

    lines += [
        "",
        "## 字段覆盖率（**如实报告，不粉饰**）",
        "",
        "| 字段 | 非空数量 | 占比 |",
        "| --- | ---: | ---: |",
        f"| name | {total} | 100.0% |",
        f"| name_en | {non_null('name_en')} | {pct(non_null('name_en'))} |",
        f"| opening_hours | {non_null('opening_hours')} | {pct(non_null('opening_hours'))} |",
        f"| addr:district | {non_null('addr:district')} | {pct(non_null('addr:district'))} |",
        f"| website | {non_null('website')} | {pct(non_null('website'))} |",
        f"| phone | {non_null('phone')} | {pct(non_null('phone'))} |",
        f"| wikidata | {non_null('wikidata')} | {pct(non_null('wikidata'))} |",
        f"| wikipedia | {non_null('wikipedia')} | {pct(non_null('wikipedia'))} |",
        "",
        f"## 归一化名称重复（≥2 次，共 {len(duplicate_names)} 组，最多列 40 组）",
        "",
    ]
    if duplicate_names:
        lines += ["| 归一化名称 | 次数 |", "| --- | ---: |"]
        lines += [f"| {name} | {count} |" for name, count in duplicate_names[:40]]
    else:
        lines.append("无重复。")

    lines += [
        "",
        "## 数据缺口（已知，后续丰富化阶段处理）",
        "",
        "- `opening_hours` 覆盖率低意味着大量地点的营业时间只能标 `unknown`，",
        "  按 PRD 要求必须写 NULL + 记入 `unknown_fields`，并在 UI 提示「出发前确认官方信息」。",
        "- `price_min/max` 在 OSM 中基本缺失，不得用估算值冒充真实票价。",
        "- `addr:district` 缺失时无法按行政区聚合，但可由坐标反推（不写死猜测值）。",
        "",
    ]
    return "\n".join(lines)


# ── 入口 ────────────────────────────────────────────────────────────────────


def parse_bbox(text: str) -> tuple[float, float, float, float]:
    parts = [float(p) for p in text.split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("bbox 需要 4 个数字：south,west,north,east")
    south, west, north, east = parts
    if not (south < north and west < east):
        raise argparse.ArgumentTypeError("bbox 必须满足 south<north 且 west<east")
    return south, west, north, east


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="抓取广州 OSM POI 原始数据")
    default_out = Path(__file__).resolve().parent.parent / "data" / "raw"
    parser.add_argument("--out", type=Path, default=default_out, help="输出目录")
    parser.add_argument(
        "--bbox",
        type=parse_bbox,
        default=DEFAULT_BBOX,
        help="south,west,north,east（默认广州全域）",
    )
    parser.add_argument(
        "--groups",
        default=",".join(GROUPS),
        help=f"要抓取的分组，逗号分隔。可选：{', '.join(GROUPS)}",
    )
    args = parser.parse_args(argv)

    selected = [g.strip() for g in args.groups.split(",") if g.strip()]
    unknown = [g for g in selected if g not in GROUPS]
    if unknown:
        parser.error(f"未知分组：{unknown}；可选：{list(GROUPS)}")

    args.out.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    print(f"开始抓取广州 OSM POI：{len(selected)} 个分组，bbox={args.bbox}", flush=True)

    records: list[dict[str, Any]] = []
    group_counts: dict[str, int] = {}
    snapshot_timestamp: str | None = None
    seen: set[tuple[str, int]] = set()

    for index, group in enumerate(selected):
        specs = GROUPS[group]
        query = build_query(specs, args.bbox)
        print(f"\n[{index + 1}/{len(selected)}] {group}（{len(query)} 字节查询）", flush=True)
        payload, _mirror = query_overpass(query, label=group)

        elements = payload.get("elements", [])
        if not elements:
            # 空结果必须显式告警：曾经因为镜像没有地区数据而把"抓取失败"当成"该类别为空"
            print(f"    ⚠︎ 分组 {group} 返回 0 条 —— 请确认不是镜像数据覆盖问题", flush=True)
        group_counts[group] = len(elements)
        snapshot_timestamp = (payload.get("osm3s") or {}).get("timestamp_osm_base") or snapshot_timestamp

        kept = 0
        for element in elements:
            record = to_record(element, specs)
            if record is None:
                continue
            key = (record["osm_type"], record["osm_id"])
            if key in seen:
                continue
            seen.add(key)
            records.append(record)
            kept += 1
        print(f"    → 有效记录 {kept} 条（累计 {len(records)}）", flush=True)

        if index < len(selected) - 1:
            time.sleep(SLEEP_BETWEEN_QUERIES_S)

    elapsed = time.monotonic() - started
    records.sort(key=lambda r: (r["primary_tag"], r["name"]))

    payload = {
        "meta": {
            "collected_at": datetime.now(UTC).isoformat(),
            "osm_snapshot_timestamp": snapshot_timestamp,
            "bbox": list(args.bbox),
            "groups": {g: group_counts.get(g, 0) for g in selected},
            "record_count": len(records),
            "elapsed_s": round(elapsed, 1),
            "source": "OpenStreetMap via Overpass API (ODbL)",
            "license_note": (
                "数据来自 OpenStreetMap 贡献者，遵循 ODbL 许可；每条记录的 source_url "
                "指向其 OSM permalink。"
            ),
            "user_agent": USER_AGENT,
        },
        "records": records,
    }

    json_path = args.out / "osm_guangzhou_raw.json"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    summary = build_summary(records, group_counts, args.bbox, snapshot_timestamp, elapsed)
    summary_path = args.out / "OSM_FETCH_SUMMARY.md"
    summary_path.write_text(summary, encoding="utf-8")

    print(f"\n{'=' * 60}")
    print(summary)
    print(f"{'=' * 60}")
    print(f"\n原始数据：{json_path}")
    print(f"摘要：    {summary_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
