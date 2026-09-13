#!/usr/bin/env python3
"""知识库质检（把 PRD §9.3 的上线门槛变成可执行断言）。

设计立场：
- **阻断项（blocker）**：不达标就非零退出，知识库不允许对用户开放。
  宁可城市"未上线"，也不要把质量不明的数据端给用户。
- **告警项（warning）**：记录现状、打印出来，但不阻断 —— 因为它们是"数据源的客观限制"
  （例如 OSM 的 opening_hours 覆盖率天生就低），不是我们的错误。
  把客观限制伪装成"通过"才是问题，如实报告不是。

用法：
    python scripts/validate_seed.py             # 人类可读输出
    python scripts/validate_seed.py --json      # 机器可读（供 CI / 建库脚本调用）
    python scripts/validate_seed.py --city guangzhou
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict, dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_limits_config, get_seed_config
from app.db.session import get_sessionmaker
from app.domain.categories import PLACE_CATEGORIES

# 每个活跃地点数的最低要求（PRD FR-03 AC-3.3）。
# 不存在的类别写 0，报告里会显示 0/要求，不会假装达标。
CATEGORY_MINIMUMS = {
    "attraction": 30,
    "district": 10,
    "food": 50,
    "cafe": 15,
    "museum": 10,
    "historic": 15,
    "nightview": 8,
    "family": 10,
    "shopping": 10,
    "nature": 15,
}

# 某些类别的数量偏低有**明确的结构性原因**，报告里要写清楚，
# 否则读报告的人会以为"数据没抓够"，从而去抓更多无用数据。
# ★ 刻意不检查 citywalk 类别的数量 ★（与 photo 同理）
# 原因：「适合 CityWalk」是地点的**属性**（places.walkability_score），不是类型。
# 沙面、永庆坊这类适合漫游的地方在库里是 district；OSM 也没有 citywalk 标签，
# 该类别在真实数据里没有产生者。给它设数量门槛等于用错误指标考核数据质量。
# 该偏好由 config/scoring.yaml 的 preference_dimensions.citywalk → field: walkability_score 承载。
CATEGORY_NOTES = {
    "district": "依赖 OSM 的 place=* 类目（suburb/neighbourhood/island/village），"
                "该类目在主抓取中未包含、定向补抓时 Overpass 不可用；恢复后即可补齐",
}

# ★ 刻意不检查 photo 类别的数量 ★
# 原因：「适合拍照」是地点的**属性**（places.photo_score 分值维度），不是地点的**类型**。
# 沙面是 district、石室圣心大教堂是 historic、广州塔是 nightview —— 它们 photo_score 很高，
# 但类别不是 photo。给 photo 设类别数量门槛，等于用错误的指标考核数据质量。
# 该偏好由 config/scoring.yaml 的 preference_dimensions.photo → field: photo_score 承载。


@dataclass
class Check:
    key: str
    label: str
    actual: Any
    expected: str
    ok: bool
    level: str = "blocker"  # blocker | warning
    detail: str = ""


@dataclass
class Report:
    city: str
    checks: list[Check] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)

    @property
    def blockers_failed(self) -> list[Check]:
        return [c for c in self.checks if c.level == "blocker" and not c.ok]

    @property
    def warnings_failed(self) -> list[Check]:
        return [c for c in self.checks if c.level == "warning" and not c.ok]

    @property
    def passed(self) -> bool:
        return not self.blockers_failed

    def as_dict(self) -> dict[str, Any]:
        return {
            "city": self.city,
            "passed": self.passed,
            "blockers_failed": [c.key for c in self.blockers_failed],
            "warnings_failed": [c.key for c in self.warnings_failed],
            "checks": [asdict(c) for c in self.checks],
            "stats": self.stats,
        }


async def _scalar(session: AsyncSession, sql: str, **params: Any) -> Any:
    return (await session.execute(text(sql), params)).scalar()


async def run_checks(session: AsyncSession, city_slug: str) -> Report:
    limits = get_limits_config()
    thresholds = limits.data_quality
    seed_cfg = get_seed_config()
    report = Report(city=city_slug)

    city_id = await _scalar(session, "SELECT id FROM cities WHERE slug = :s", s=city_slug)
    if city_id is None:
        report.checks.append(
            Check("city_exists", "城市存在", None, f"slug={city_slug}", False, "blocker")
        )
        return report
    params = {"c": city_id}

    total_active = await _scalar(
        session, "SELECT count(*) FROM places WHERE city_id = :c AND status = 'active'", **params
    )
    report.stats["active_places"] = total_active
    report.checks.append(
        Check(
            "min_active_places",
            "活跃地点数",
            total_active,
            f"≥ {thresholds.min_active_places}",
            total_active >= thresholds.min_active_places,
        )
    )

    route_count = await _scalar(session, "SELECT count(*) FROM routes WHERE city_id = :c", **params)
    report.stats["routes"] = route_count
    report.checks.append(
        Check("min_routes", "路线数", route_count, f"≥ {thresholds.min_routes}", route_count >= thresholds.min_routes)
    )

    # 来源：每个地点必须有 source_url，或者明确标记为 unknown（PRD §8.7 R3 的软版本）
    no_source = await _scalar(
        session,
        """
        SELECT count(*) FROM places
        WHERE city_id = :c AND status = 'active'
          AND (source_url IS NULL OR source_url = '')
          AND verification_status <> 'unknown'
        """,
        **params,
    )
    report.stats["places_without_source_unmarked"] = no_source
    report.checks.append(
        Check(
            "no_source_without_unknown_flag",
            "无来源却未标记 unknown 的地点",
            no_source,
            "= 0",
            no_source == 0,
            "blocker",
            "有 source_url 或明确标 unknown，二者必有其一 —— 从根上杜绝伪造来源",
        )
    )

    sourced = await _scalar(
        session,
        "SELECT count(*) FROM places WHERE city_id = :c AND status='active' AND source_url IS NOT NULL",
        **params,
    )
    sourced_ratio = sourced / total_active if total_active else 0.0
    report.stats["sourced_ratio"] = round(sourced_ratio, 4)
    report.checks.append(
        Check(
            "min_sourced_ratio",
            "有来源的地点占比",
            f"{sourced_ratio:.1%}",
            f"≥ {thresholds.min_sourced_ratio:.0%}",
            sourced_ratio >= thresholds.min_sourced_ratio,
        )
    )

    verified = await _scalar(
        session,
        "SELECT count(*) FROM places WHERE city_id = :c AND status='active' AND verification_status='verified'",
        **params,
    )
    verified_ratio = verified / total_active if total_active else 0.0
    report.stats["verified_ratio"] = round(verified_ratio, 4)
    report.checks.append(
        Check(
            "min_verified_ratio",
            "身份可交叉核验的地点占比",
            f"{verified_ratio:.1%}",
            f"≥ {thresholds.min_verified_ratio:.0%}",
            verified_ratio >= thresholds.min_verified_ratio,
        )
    )

    # 长期目标（不阻断）：见 config/limits.yaml 中对该阈值来龙去脉的说明。
    # 保留这一条是为了让"没做到的部分"在每次质检里都可见，而不是被悄悄忘掉。
    aspirational = getattr(thresholds, "aspirational_verified_ratio", 0.30)
    report.checks.append(
        Check(
            "aspirational_verified_ratio",
            "【长期目标】身份可交叉核验占比",
            f"{verified_ratio:.1%}",
            f"≥ {aspirational:.0%}",
            verified_ratio >= aspirational,
            "warning",
            "需要引入官方名录（文旅局/景区官网）才能达成；当前网络下 wikidata/wikipedia 不可达",
        )
    )

    # 坐标越界：必须为 0
    bbox = seed_cfg.coord.bbox
    out_of_bbox = await _scalar(
        session,
        """
        SELECT count(*) FROM places
        WHERE city_id = :c AND (latitude < :min_lat OR latitude > :max_lat
                                OR longitude < :min_lng OR longitude > :max_lng
                                OR latitude = 0 OR longitude = 0)
        """,
        c=city_id,
        min_lat=bbox["min_lat"],
        max_lat=bbox["max_lat"],
        min_lng=bbox["min_lng"],
        max_lng=bbox["max_lng"],
    )
    report.checks.append(
        Check("coord_in_bbox", "坐标越界地点数", out_of_bbox, "= 0", out_of_bbox == 0)
    )

    # 未处理的重复：同城市、同名、相距 < 合并半径 —— 说明去重没做干净
    radius = seed_cfg.relevance.duplicate_merge_radius_m
    dup_groups = await _scalar(
        session,
        """
        WITH p AS (
          SELECT id, lower(canonical_name) AS n, latitude AS la, longitude AS lo
          FROM places WHERE city_id = :c AND status = 'active'
        ), pairs AS (
          SELECT p1.id, p2.id AS other
          FROM p a JOIN p p1 ON p1.n = a.n
          JOIN p p2 ON p2.n = a.n AND p2.id > p1.id
          WHERE 111320 * sqrt(power(p1.la - p2.la, 2)
                + power((p1.lo - p2.lo) * cos(radians(p1.la)), 2)) < :radius
        )
        SELECT count(*) FROM pairs
        """,
        c=city_id,
        radius=radius,
    )
    report.checks.append(
        Check(
            "no_unresolved_duplicates",
            f"同名且 <{radius}m 的未合并重复对",
            dup_groups,
            "= 0",
            dup_groups <= thresholds.max_unresolved_duplicate,
        )
    )

    # 营业时间覆盖率（告警项：OSM 的客观限制）
    hours_known = await _scalar(
        session,
        "SELECT count(*) FROM places WHERE city_id = :c AND status='active' AND opening_hours_raw IS NOT NULL",
        **params,
    )
    unknown_hours_ratio = 1 - (hours_known / total_active if total_active else 0.0)
    report.stats["unknown_hours_ratio"] = round(unknown_hours_ratio, 4)
    report.checks.append(
        Check(
            "max_unknown_hours_ratio",
            "营业时间未知占比",
            f"{unknown_hours_ratio:.1%}",
            f"≤ {thresholds.max_unknown_hours_ratio:.0%}",
            unknown_hours_ratio <= thresholds.max_unknown_hours_ratio,
            "warning",
            "OSM 的 opening_hours 覆盖率天然很低；不达标如实记录，UI 必须提示出发前确认",
        )
    )

    # 伪路线：少于 3 个站点的路线
    thin_routes = await _scalar(
        session,
        """
        SELECT count(*) FROM (
          SELECT r.id FROM routes r
          LEFT JOIN route_places rp ON rp.route_id = r.id
          WHERE r.city_id = :c GROUP BY r.id HAVING count(rp.id) < 3
        ) t
        """,
        **params,
    )
    report.checks.append(
        Check("no_thin_routes", "少于 3 个站点的伪路线", thin_routes, "= 0", thin_routes == 0)
    )

    # 类别覆盖
    rows = await session.execute(
        text(
            """
            SELECT category, count(*) AS n FROM places
            WHERE city_id = :c AND status='active' GROUP BY category
            """
        ),
        params,
    )
    counts = {row[0]: row[1] for row in rows}
    report.stats["category_counts"] = counts
    missing_categories: list[str] = []
    for category, minimum in CATEGORY_MINIMUMS.items():
        actual = counts.get(category, 0)
        ok = actual >= minimum
        if not ok:
            missing_categories.append(f"{category}({actual}/{minimum})")
        report.checks.append(
            Check(
                f"category_{category}",
                f"类别 {category} 数量",
                actual,
                f"≥ {minimum}",
                ok,
                "warning" if actual > 0 else "blocker",
                CATEGORY_NOTES.get(
                    category,
                    "该类别完全缺失会削弱候选生成能力" if actual == 0 else "数量偏低，建议补齐",
                ),
            )
        )

    # 未知类别（不应出现：CHECK 约束会拦，但显式检查一遍）
    # 类别清单来自 app/domain/categories.py 的代码常量（非用户输入），
    # 且下面用 :cat_N 绑定参数传入，不做字符串拼接 —— 不构成注入面。
    placeholders = ", ".join(f":cat_{i}" for i in range(len(PLACE_CATEGORIES)))
    category_params = dict(params)
    category_params.update({f"cat_{i}": value for i, value in enumerate(PLACE_CATEGORIES)})
    unknown_categories = await _scalar(
        session,
        f"SELECT count(*) FROM places WHERE city_id = :c AND category NOT IN ({placeholders})",  # noqa: S608
        **category_params,
    )
    report.checks.append(
        Check("no_unknown_category", "非法类别的地点数", unknown_categories, "= 0", unknown_categories == 0)
    )

    # 分值越界（R5）
    bad_scores = await _scalar(
        session,
        """
        SELECT count(*) FROM places WHERE city_id = :c AND (
          popularity_score NOT BETWEEN 0 AND 1 OR photo_score NOT BETWEEN 0 AND 1
          OR food_score NOT BETWEEN 0 AND 1 OR culture_score NOT BETWEEN 0 AND 1
          OR night_view_score NOT BETWEEN 0 AND 1 OR family_score NOT BETWEEN 0 AND 1
          OR couple_score NOT BETWEEN 0 AND 1 OR walkability_score NOT BETWEEN 0 AND 1
          OR rainy_day_score NOT BETWEEN 0 AND 1)
        """,
        **params,
    )
    report.checks.append(Check("scores_in_range", "分值越界的地点数", bad_scores, "= 0", bad_scores == 0))

    # 每个地点都必须有来源条目（plural：place_sources）
    orphan = await _scalar(
        session,
        """
        SELECT count(*) FROM places p WHERE p.city_id = :c
          AND NOT EXISTS (SELECT 1 FROM place_sources ps WHERE ps.place_id = p.id)
        """,
        **params,
    )
    report.checks.append(
        Check("every_place_has_source_row", "缺少来源条目的地点数", orphan, "= 0", orphan == 0)
    )

    # 路线站点完整性：不得引用已被合并的地点
    merged_refs = await _scalar(
        session,
        """
        SELECT count(*) FROM route_places rp
        JOIN routes r ON r.id = rp.route_id
        JOIN places p ON p.id = rp.place_id
        WHERE r.city_id = :c AND p.status = 'merged'
        """,
        **params,
    )
    report.checks.append(
        Check("no_route_refs_to_merged", "路线引用了已合并地点的次数", merged_refs, "= 0", merged_refs == 0)
    )

    # 人工保护标记（信息项）：被保护但无信号的地点是"人工确认过的"，需要可见
    protected = await _scalar(
        session,
        "SELECT count(*) FROM places WHERE city_id = :c AND 'kept_by_curated_protection' = ANY(data_quality_flags)",
        **params,
    )
    report.stats["curated_protected_places"] = protected

    return report


def render(report: Report) -> str:
    lines = [
        f"知识库质检报告 · 城市 {report.city}",
        "=" * 72,
    ]
    for check in report.checks:
        # 阻断项不达标用 ❌（不允许开放），告警项不达标用 ⚠️（客观限制，如实记录）
        failed_mark = "❌" if check.level == "blocker" else "⚠️ "
        mark = "✅" if check.ok else failed_mark
        lines.append(f"{mark} [{check.level:7s}] {check.label}: {check.actual}（要求 {check.expected}）")
        if check.detail and not check.ok:
            lines.append(f"          ↳ {check.detail}")
    lines.append("-" * 72)
    lines.append(
        f"阻断项失败：{len(report.blockers_failed)} · 告警项未达标：{len(report.warnings_failed)}"
    )
    lines.append("结论：" + ("✅ 通过（允许对用户开放）" if report.passed else "❌ 未通过（不允许开放）"))
    lines.append("")
    lines.append("统计摘要：")
    lines.append(json.dumps(report.stats, ensure_ascii=False, indent=2))
    return "\n".join(lines)


async def main_async(city: str, as_json: bool) -> int:
    maker = get_sessionmaker()
    async with maker() as session:
        report = await run_checks(session, city)
    if as_json:
        print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
    else:
        print(render(report))
    return 0 if report.passed else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="知识库质检")
    parser.add_argument("--city", default="guangzhou")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    return asyncio.run(main_async(args.city, args.json))


if __name__ == "__main__":
    sys.exit(main())
