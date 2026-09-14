#!/usr/bin/env python3
"""成本报告：从 ``cost_logs`` 汇总近 N 天的外部调用花费。

★ 这个脚本的第一职责不是"报告成本很低"，而是**如实标注成本是否可信** ★
``config/pricing.yaml`` 里还有未校准的单价（DeepSeek / 高德 / Serper）。
未校准时 ``cost_logs.amount_cny`` 记的是 0，因此报表必须显式写出
"含未校准单价，金额不代表真实支出" —— 否则读报表的人会把 0 当成"没花钱"。

用法：
    python scripts/cost_report.py                 # 近 7 天
    python scripts/cost_report.py --days 30
    python scripts/cost_report.py --json          # 机器可读
    python scripts/cost_report.py --no-write      # 不生成 docs/COST_REPORT.md
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

from app.core.paths import docs_dir
from app.db.session import get_sessionmaker
from app.services.cost import CostStore

DEFAULT_DAYS = 7


async def collect(days: int) -> dict[str, Any]:
    async with get_sessionmaker()() as session:
        store = CostStore(session)
        return {
            "summary": await store.summary(days=days),
            "by_provider": await store.by_provider(days=days),
        }


def render_markdown(report: dict[str, Any]) -> str:
    summary = report["summary"]
    lines = [
        "# COST_REPORT.md — 成本实测报告（脚本生成）",
        "",
        f"> 统计窗口：近 **{summary['days']}** 天 ｜ 生成命令：`make cost`",
        "",
        f"**成本可信度**：{'✅ 全部单价已校准' if summary['pricing_calibrated'] else '⚠️ ' + summary['note']}",
        "",
        "## 按类别",
        "",
        "| 类别 | 调用次数 | 缓存命中 | 金额（元） |",
        "| --- | ---: | ---: | ---: |",
    ]
    if not summary["by_category"]:
        lines.append("| _（窗口内没有成本记录）_ | 0 | 0 | 0 |")
    for row in summary["by_category"]:
        lines.append(
            f"| {row['category']} | {row['calls']} | {row['cache_hits']} | {row['amount_cny']} |"
        )
    lines += [
        "",
        "## 按 Provider",
        "",
        "| Provider | 类别 | 调用次数 | 缓存命中 | 金额（元） |",
        "| --- | --- | ---: | ---: | ---: |",
    ]
    if not report["by_provider"]:
        lines.append("| _（窗口内没有成本记录）_ | - | 0 | 0 | 0 |")
    for row in report["by_provider"]:
        lines.append(
            f"| {row['provider']} | {row['category']} | {row['calls']} | "
            f"{row['cache_hits']} | {row['amount_cny']} |"
        )
    lines += [
        "",
        "## 读报表前必看",
        "",
        f"- 未校准记录数：**{summary['uncalibrated_rows']}**。"
        "这类记录写入时对应单价是 `null`（或在 `config/pricing.yaml` 校准之前就已落库），"
        "计算时按 0 记入，所以这些行的金额**不代表真实支出**。",
        "- 因此 `pricing_calibrated=false` 的常见原因不是“现在还有 null”，"
        "而是**窗口内混着校准前的旧记录**；清掉测试数据或换个干净库重跑就会变成 ✅。",
        "- 熔断并不依赖金额：`plan_llm_calls` / `plan_map_calls` / 搜索查询数都是"
        "**按次数**的硬上限，即使单价未校准也能兜住失控调用。",
        "- 校准流程见 `config/pricing.yaml` 顶部说明；校准后重新跑一次真实调用核对 token 数与账单。",
        "",
    ]
    return "\n".join(lines)


def render_text(report: dict[str, Any]) -> str:
    summary = report["summary"]
    out = [
        f"── 成本报告（近 {summary['days']} 天）──",
        f"pricing_calibrated = {summary['pricing_calibrated']}  （{summary['note']}）",
        "",
        "类别            调用   缓存命中      金额(元)",
    ]
    for row in summary["by_category"]:
        out.append(
            f"{row['category']:<10} {row['calls']:>6}   {row['cache_hits']:>8}   {row['amount_cny']:>10}"
        )
    if not summary["by_category"]:
        out.append("（窗口内没有成本记录）")
    out.append("")
    out.append("按 Provider：")
    for row in report["by_provider"]:
        out.append(
            f"  {row['provider']:<12} {row['category']:<8} calls={row['calls']:<5} "
            f"hits={row['cache_hits']:<5} amount={row['amount_cny']}"
        )
    return "\n".join(out)


async def run(days: int, *, write_markdown: bool) -> int:
    try:
        report = await collect(days)
    except Exception as exc:  # pragma: no cover - 依赖真实数据库
        print(f"❌ 无法读取 cost_logs：{type(exc).__name__}: {exc}", file=sys.stderr)
        print("   请确认数据库已启动并迁移（make migrate）。", file=sys.stderr)
        return 1
    print(render_text(report))
    if write_markdown:
        target = docs_dir() / "COST_REPORT.md"
        target.write_text(render_markdown(report), encoding="utf-8")
        print(f"\n已写入 {target}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TripDecider 成本报告")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    parser.add_argument("--json", action="store_true", help="输出 JSON（不写文件）")
    parser.add_argument("--no-write", action="store_true", help="不生成 docs/COST_REPORT.md")
    args = parser.parse_args(argv)

    if args.json:
        report = asyncio.run(collect(args.days))
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    return asyncio.run(run(args.days, write_markdown=not args.no_write))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
