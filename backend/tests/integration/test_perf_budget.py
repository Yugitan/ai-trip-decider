"""性能门槛的**秒级**回归（PRD §23.6、§26 验收第 19 项）。

为什么有了 `scripts/benchmark.py` 还要这个文件：
    benchmark.py 是"分钟级 + 出一份报告"的压测，**刻意不进 `make check`** ——
    它进来，提交前的门槛就会变成没人愿意跑的门槛（等于没有）。
    这里把同几条门槛变成秒级断言：冷规划 p95、束搜索、以及"报告本身没有说假话"。
    它不会取代压测报告（p95 要 10 个样本、并发要 20 条才有意义，
    这些都留在脚本里），但**门槛真的被守住了**，而不是只在某次手动运行时是绿的。

★ 两条口径上的约定（都是这个仓库里踩过的坑）★

1. **分位数与压测脚本共用一份实现**（`benchmark.percentile_nearest_rank`）。
   自己再写一遍就是两套口径，迟早出现"报告说 p95 = 307ms，测试按另一套算法判绿"。
   `benchmark.py` 的文件头早就写着这条约定，之前它只是一句承诺。

2. **门槛数值按 PRD 原文抄在这里，再与脚本里的常量互相钉住**。
   直接从脚本引常量会导致一个自指的闸门：把脚本的阈值从 8s 改成 30s 测试照样绿。
   所以：PRD 数值在这边写死、脚本的常量与它比对；改任何一边都会红。

顺带守住"报告不说假话"：**没测到的门槛不许渲染成 ✅**（并发那条曾经因为没有
`target_ms` 而被判成达标，首页 TTI 曾经拿 LCP 的读数打勾）。
"""

from __future__ import annotations

import asyncio
import itertools
import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import get_settings
from app.db.models import RateLimitCounter
from scripts import benchmark

pytestmark = pytest.mark.integration

#: PRD §23.6 的门槛，**按原文抄写**（不是从被测脚本里引 —— 理由见文件头第 2 条）。
PRD_BUDGETS_MS: dict[str, int] = {
    "COLD_PLAN_TARGET_MS": 8_000,
    "CACHE_HIT_TARGET_MS": 1_500,
    "REVISE_TARGET_MS": 3_000,
    "BEAM_SEARCH_TARGET_MS": 500,
    "SHARE_LCP_TARGET_MS": 2_500,
    "HOME_TTI_TARGET_MS": 3_000,
    "SLOW_STATEMENT_TARGET_MS": 200,
}

#: 冷规划只测 3 次。门槛是 p95，样本少说明它此刻就是"最慢的那一次" ——
#: 这个文件守的是"没有系统性的慢"，不是统计学结论（后者归 benchmark.py）。
COLD_SAMPLES = 3

_counter = itertools.count(1)


def _clear_rate_limits() -> None:
    """限流计数器是状态不是业务数据：不清掉，同一天的配额会被整套用例共用。

    默认"每 IP 每天 5 次冷规划"，本文件要发 3 次冷规划，第一次跑通过、
    第二次跑就会全被 429 挡住（与 `test_trips_api.py` 同一个坑）。
    """
    url = get_settings().database_url

    async def run() -> None:
        engine = create_async_engine(url)
        try:
            async with engine.begin() as conn:
                await conn.execute(delete(RateLimitCounter))
        finally:
            await engine.dispose()

    asyncio.run(run())


@pytest.fixture(autouse=True)
def _fresh_rate_limits() -> None:
    _clear_rate_limits()


def _cold_payload() -> dict[str, Any]:
    """每次的参数都必须与历史不同。

    `plan_cache` 是**全局内容寻址**的：同参数会命中缓存（甚至跨会话），
    量到的就是"复制一份行程"而不是"规划一次"。参数里带一个每轮变化的基数，
    并逐条断言 `meta.cached is False` —— 否则 p95 会好看得离谱。
    """
    base = uuid.uuid4().int % 90_000 + 10_000
    return {
        "city": "guangzhou",
        "days": 1,
        "people": 2,
        "preferences": ["food"],
        "pace": "relaxed",
        "budget": {"amount": base + next(_counter), "scope": "per_person"},
        "free_text": "",
    }


def _row(report: str, label: str) -> str:
    for line in report.splitlines():
        if line.startswith(f"| {label} |"):
            return line
    raise AssertionError(f"报告里没有 {label} 这一行：\n{report}")


# ── 一、门槛本身：脚本常量 ↔ PRD ────────────────────────────────────────────


def test_benchmark_thresholds_match_prd() -> None:
    """压测脚本里的门槛必须与 PRD 逐条相等（放宽门槛比测不到更危险）。"""
    drifted = {
        name: {"脚本": getattr(benchmark, name), "PRD": expected}
        for name, expected in PRD_BUDGETS_MS.items()
        if getattr(benchmark, name) != expected
    }
    assert not drifted, f"压测脚本的门槛与 PRD 不一致：{drifted}"
    assert benchmark.FEATURE_JS_TARGET_BYTES == 200 * 1024, "首屏 JS 门槛是 200KB（gzip 后）"
    assert benchmark.CONCURRENCY == 20, "PRD 要求验证 20 并发"


def test_percentile_is_nearest_rank() -> None:
    """分位数口径：rank = ceil(q·n)，且 n 很小时 p95 就是"最大的那几个样本"。

    报告开头写的是这条口径（它必须与实现一致 —— 曾经写成"n=10 时 p95 是第二大的样本"，
    而实现给出的是最大的那个）。这里把两个样本量都钉住。
    """
    ten = [float(value) for value in range(1, 11)]  # 1..10
    twenty = [float(value) for value in range(1, 21)]  # 1..20
    assert benchmark.percentile_nearest_rank(ten, 0.95) == 10.0
    assert benchmark.percentile_nearest_rank(twenty, 0.95) == 19.0
    assert benchmark.percentile_nearest_rank([7.0], 0.95) == 7.0
    assert benchmark.percentile_nearest_rank(ten, 0.5) == 5.0

    sample = benchmark.Sample("x", "x", 1_000, "x")
    sample.values.extend(ten)
    assert sample.p95 == benchmark.percentile_nearest_rank(ten, 0.95), "Sample 必须走同一份实现"


def test_a_threshold_we_did_not_measure_is_never_rendered_as_passed() -> None:
    """报告里"没测到"不许出现在"达标"那一列。

    两个真实案例：首页 TTI（PRD 有门槛，但我们只量了 LCP）与并发 20
    （它的判据是"没有 5xx"，不是耗时，曾经因为没设 `target_ms` 而被渲染成 ✅）。
    """
    tti = benchmark.Sample("home_tti", "首页 TTI", 3_000, "未测（需长任务分析）", skipped=True)
    unjudged = benchmark.Sample("concurrency_20", "并发 20", None, "asyncio.gather(20)")
    unjudged.values.append(1_234.0)
    judged = benchmark.Sample("concurrency_20（有结论）", "并发 20（有结论）", None, "asyncio.gather(20)")
    judged.values.append(1_234.0)
    judged.auto_ok = False

    report = benchmark.render_report([tti, unjudged, judged], {}, {}, generated_at="测试")
    assert "⏭ 不可测" in _row(report, "首页 TTI")
    assert "✅" not in _row(report, "首页 TTI")
    assert "—（无门槛）" in _row(report, "并发 20")
    assert "✅" not in _row(report, "并发 20")
    assert "❌" in _row(report, "并发 20（有结论）")
    assert unjudged.ok is None, "没有门槛、也没给结论的项，结论必须是 None 而不是 True"


def test_build_log_does_not_add_shared_bundle_twice(tmp_path: Path) -> None:
    """`next build` 的 First Load JS **已经含 shared**，不能再加一遍。

    第一版写成"shared + 首页"，把一份 127 kB 的首屏算成 229 kB 并判超门槛 ——
    一个只因重复计算而超标的门槛会让人去优化不存在的问题。
    """
    log = tmp_path / "build.log"
    log.write_text(
        "Route (app)                                 Size  First Load JS\n"
        "┌ ○ /                                     9.4 kB         127 kB\n"
        "├ ○ /about/data                          1.82 kB         109 kB\n"
        "+ First Load JS shared by all             102 kB\n",
        encoding="utf-8",
    )
    parsed = benchmark.parse_build_log(log)
    assert parsed["home_kb"] == 127.0, "取的是首页那一行的 First Load JS，不是 9.4 + 102"
    assert parsed["shared_kb"] == 102.0
    assert parsed["pages_kb"]["/about/data"] == 109.0


# ── 二、真的跑一遍：冷规划与束搜索 ─────────────────────────────────────────


def test_cold_plan_p95_stays_within_budget(client: TestClient) -> None:
    """端到端一次真实规划（知识库 + 编排 + 落库）不许越过 8s。"""
    values: list[float] = []
    for _ in range(COLD_SAMPLES):
        response = client.post("/api/v1/trips:plan", params={"sync": "true"}, json=_cold_payload())
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["ok"] is True
        meta = body["meta"]
        assert meta["cached"] is False, (
            "命中了 plan_cache —— 这不再是冷启动测量，数字不能当门槛用"
            f"（参数基数重复了？）：{meta}"
        )
        elapsed = meta["elapsed_ms"]
        assert isinstance(elapsed, (int, float)), f"响应里没有后端计时 meta.elapsed_ms：{meta}"
        values.append(float(elapsed))

    p95 = benchmark.percentile_nearest_rank(values, 0.95)
    assert p95 <= PRD_BUDGETS_MS["COLD_PLAN_TARGET_MS"], (
        f"冷规划 p95 = {p95:.0f}ms 超过门槛 {PRD_BUDGETS_MS['COLD_PLAN_TARGET_MS']}ms"
        f"（样本 {values}）"
    )


async def test_beam_search_stays_within_budget() -> None:
    """束搜索（80 候选 × 三种 archetype）纯计算部分 < 500ms。

    直接复用压测脚本的测量函数：这条门槛量的是算法本身，
    如果这里另写一份取候选/取关系的代码，"测试绿而报告超"就会变成可能，
    而两者本来测的是同一件事。
    """
    from app.db.session import dispose_engines

    # ★ 先释放、后新建 ★
    # 同一个文件里前面的用例用 TestClient 跑过请求，那份引擎绑在 TestClient 自己的
    # 事件循环上；直接复用会撞 "got Future attached to a different loop"
    # （M1 的 /health 踩过同一个坑）。测量完再释放一次，别把连接留给下一条用例。
    await dispose_engines()
    try:
        sample, facts = await benchmark.measure_beam_search()
    finally:
        await dispose_engines()

    assert facts["candidates"] > 0, "测试库里没有候选地点，等于什么都没测"
    assert sample.values, "测量函数没有产出样本"
    worst = max(sample.values)
    assert worst <= PRD_BUDGETS_MS["BEAM_SEARCH_TARGET_MS"], (
        f"束搜索最慢一次 {worst:.0f}ms 超过门槛"
        f" {PRD_BUDGETS_MS['BEAM_SEARCH_TARGET_MS']}ms（样本 {sorted(sample.values)}）"
    )
    assert not any(isinstance(v, bool) for v in sample.values)


# ── 三、顺手钉住报告里的两处措辞（它们是给人读数字的地方）──────────────────


def test_first_load_js_is_printed_in_kb_not_ms() -> None:
    """首屏 JS 的样本值是字节，报告里不能跟着别人一起打印成毫秒。

    实测过：50/50 那一行写的是 "127.0 KB ✅"，明细里却是 "最小 130048 ms" ——
    同一个数字在同两行的单位不同，读者只会把它当成又一个看不懂的数。
    """
    bundle = benchmark.Sample("first_load_js", "首屏 JS", 200 * 1024, "next build 输出")
    bundle.values.append(127.0 * 1024)
    report = benchmark.render_report([bundle], {}, {}, generated_at="测试")
    assert "127.0 KB" in report
    assert "130048 ms" not in report, "字节数不许被当成毫秒"
    assert "130048" not in report


def test_report_states_the_sample_size_next_to_every_percentile() -> None:
    """报告必须同时给出样本量与 p95：n=10 的 p95 不是统计学结论。"""
    sample = benchmark.Sample("plan_cold", "规划（冷启动，无搜索）", 8_000, "10 个不同参数")
    sample.values.extend([100.0 + index for index in range(10)])
    report = benchmark.render_report([sample], {}, {}, generated_at="测试")
    body = report.split("### 规划（冷启动，无搜索）")[1]
    assert "样本 n=10" in body
    assert "**p95" in body
