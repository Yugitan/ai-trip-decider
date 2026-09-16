#!/usr/bin/env python3
"""性能压测与门槛核对（PRD §23.6、§26 验收第 19 项）。

为什么是独立脚本而不是 pytest 用例：
    1. 它要跑几十次真实规划与 20 并发，天然是"分钟级"，塞进 `make check`
       会让提交前的门槛变得没人愿意等（而门槛一旦没人跑，就等于没有）。
    2. 它的产物是**一份报告**（`docs/PERF_REPORT.md`），而不只是退出码 ——
       "p95 是多少、样本几个、超了没有"必须能被人回看，而不是只在 CI 日志里。
       `tests/integration/test_perf_budget.py` 负责把同一条门槛变成断言，
       两者共用这里的分位函数，口径不会漂。

★ 三条方法论，写在最前面以免读数字的人误会 ★

1. **冷规划默认关掉 LLM，另记一条开 LLM 的观察值。**
   PRD 的门槛是"冷启动、无搜索 p95 ≤ 8s"。但把 DeepSeek 的往返算进门槛，
   阈值就会随第三方当天的心情浮动 —— 那不是我们能守的指标。因此：
   门槛用"规则引擎 + 知识库"（确定性、可复现）判，同时如实报告
   "开着 LLM 时是多少、慢在哪"，让人能一眼看出是不是被上游拖垮。

2. **"含搜索"这项必须用真 Key 才有意义**，所以它单独一段、默认跳过，
   只在检测到 `TAVILY_API_KEY` 时跑（3 个场景，会真的花钱但金额极小）。

3. **分位数用最近秩（nearest-rank），并把样本量与最小值/中位数一起报出来。**
   n=10 时 p95 实际就是"第二大的那个样本"，单独看它会显得过于精确。
   报出样本量是让人自己判断可信度，而不是把 10 个样本包装成统计学结论。

用法：
    cd backend && PYTHONPATH=. uv run python scripts/benchmark.py
    cd backend && PYTHONPATH=. uv run python scripts/benchmark.py --skip-search
    # 带上前端首屏 JS（由 make perf 采集的 next build 日志）
    ... --frontend-build-log ../.perf/next-build.log
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ── 门槛（PRD §23.6 逐字对应）───────────────────────────────────────────────

COLD_PLAN_TARGET_MS = 8_000
CACHE_HIT_TARGET_MS = 1_500
SEARCH_PLAN_TARGET_MS = 15_000
REVISE_TARGET_MS = 3_000
BEAM_SEARCH_TARGET_MS = 500
SHARE_LCP_TARGET_MS = 2_500
HOME_TTI_TARGET_MS = 3_000
SLOW_STATEMENT_TARGET_MS = 200
FEATURE_JS_TARGET_BYTES = 200 * 1024
CONCURRENCY = 20

COLD_SAMPLES = 10
CACHE_HIT_SAMPLES = 20
REVISE_SAMPLES = 20
SEARCH_SAMPLES = 3


def percentile_nearest_rank(samples: Sequence[float], q: float) -> float:
    """最近秩分位数（与 pytest 用例共用这一份实现，口径不会漂）。"""
    if not samples:
        raise ValueError("样本为空")
    ordered = sorted(samples)
    # 最近秩：rank = ceil(q * n)，1-based
    rank = max(1, min(len(ordered), int(-(-q * len(ordered) // 1))))
    return ordered[rank - 1]


@dataclass
class Sample:
    key: str
    label: str
    target_ms: int | None
    method: str
    values: list[float] = field(default_factory=list)
    note: str = ""
    #: 门槛本身没法有效测量（不是"慢"也不是"快"）。单独一个状态，
    #: 免得把"没测到"渲染成"达标"—— 那比缺一项指标更糟。
    skipped: bool = False
    #: 门槛不是"耗时 ≤ 某毫秒"的项（例如并发那条是"没有 5xx"），
    #: 由测量方显式给出结论。**没给结论就是"没结论"**，
    #: 不能因为"没设 target_ms"就渲染成一个 ✅ —— 那与"没测到却报达标"是同一个谎。
    auto_ok: bool | None = None

    @property
    def p95(self) -> float | None:
        return percentile_nearest_rank(self.values, 0.95) if self.values else None

    @property
    def median(self) -> float | None:
        return percentile_nearest_rank(self.values, 0.5) if self.values else None

    @property
    def ok(self) -> bool | None:
        if self.target_ms is None:
            return self.auto_ok
        if self.p95 is None:
            return None
        return self.p95 <= self.target_ms


# ── 环境准备 ────────────────────────────────────────────────────────────────


def _force_env(**values: str) -> None:
    """压测必须跑在确定的 Provider 组合上，不能受开发机 `.env` 影响。"""
    for key, value in values.items():
        os.environ[key] = value
    from app.core.config import clear_config_cache

    clear_config_cache()


def _raise_rate_limits() -> None:
    """把限流上限临时抬高。

    ``limits.yaml`` 的"每 IP 每天 5 次冷规划"是**产品规则**（防刷量），
    不是性能上限；不抬高的话第 6 个样本就会拿到 429，
    测到的是限流器而不是规划耗时（这个坑 `test_fault_injection` 里已经踩过一次）。
    """
    from app.api.v1 import trips as trips_module
    from app.core.config import get_limits_config

    base = get_limits_config()
    generous = base.model_copy(
        update={
            "rate_limit": base.rate_limit.model_copy(
                update={
                    "requests_per_ip_per_minute": 0,  # 0 = 关闭
                    "cold_plans_per_ip_per_day": 0,
                    "cold_plans_per_session_per_day": 0,
                    "revisions_per_session_per_hour": 0,
                }
            )
        }
    )
    # 直接赋值与 setattr 都会被 mypy 拦下（trips 模块没把这个名字声明为公开导出，
    # 而它本来还是个带缓存的函数对象）。这里要的正是「临时换掉这个模块里的引用」，
    # 所以走 __dict__ 绕开静态检查是刻意为之，不是偷懒。
    trips_module.__dict__["get_limits_config"] = lambda: generous


async def _clear_rate_counters() -> None:
    from sqlalchemy import delete

    from app.db.models import RateLimitCounter
    from app.db.session import get_sessionmaker

    async with get_sessionmaker()() as session:
        await session.execute(delete(RateLimitCounter))
        await session.commit()


def _payload(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "city": "guangzhou",
        "days": 1,
        "people": 2,
        "preferences": ["food"],
        "pace": "relaxed",
        "budget": {"amount": 400, "scope": "per_person"},
        "free_text": "",
    }
    base.update(overrides)
    return base


# ── 一、后端：p95 ───────────────────────────────────────────────────────────


async def _plan(client: Any, payload: dict[str, Any]) -> tuple[int, dict[str, Any], float]:
    started = time.perf_counter()
    response = await client.post("/api/v1/trips:plan", params={"sync": "true"}, json=payload)
    wall_ms = (time.perf_counter() - started) * 1000
    return response.status_code, response.json(), wall_ms


def _server_ms(body: dict[str, Any], fallback: float) -> float:
    """优先用响应里的 ``meta.elapsed_ms``（后端自己的计时，PRD 把这项定为后端 metric）。

    缺失时退回客户端墙钟，并在报告里注明 —— 两种口径不能混着比。
    """
    meta = body.get("meta") or {}
    value = meta.get("elapsed_ms")
    return float(value) if isinstance(value, (int, float)) else fallback


async def measure_backend(args: argparse.Namespace) -> tuple[list[Sample], dict[str, Any]]:
    import httpx

    from app.main import create_app

    samples: list[Sample] = []
    facts: dict[str, Any] = {}

    # ★ 每一轮的参数都必须与历史不同，否则"冷规划"量的其实是缓存的拷贝 ★
    # plan_cache 是**全局内容寻址**的（同参数跨会话也命中，命中后直接复制一份 trip）。
    # 第一版没有这个偏移量，于是同一台机器上跑第二遍时全部样本都是命中缓存：
    # 冷 p95 与缓存命中 p95 一模一样、20 并发只花 165ms —— 数字好看但是假的。
    # 用一轮一变的基数（写进报告，保证可复现），并逐个断言 `meta.cached is False`。
    run_base = uuid.uuid4().int % 90_000 + 10_000
    facts["run_seed"] = run_base

    cold = Sample("plan_cold", "规划（冷启动，无搜索）", COLD_PLAN_TARGET_MS, f"{COLD_SAMPLES} 个不同参数")
    cache_hit = Sample("plan_cache_hit", "规划（缓存命中）", CACHE_HIT_TARGET_MS, f"同一参数重复 {CACHE_HIT_SAMPLES} 次")
    revise = Sample("revise", "修改（自然语言改路线）", REVISE_TARGET_MS, f"{REVISE_SAMPLES} 次修改")
    search = Sample("plan_with_search", "规划（含搜索）", SEARCH_PLAN_TARGET_MS, f"{SEARCH_SAMPLES} 个需搜索场景")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()), base_url="http://perf.local"
    ) as client:
        # ── 冷规划 ──
        cache_layers: dict[str, int] = {}
        for index in range(COLD_SAMPLES):
            payload = _payload(
                budget={"amount": run_base + index * 17, "scope": "per_person"},
                preferences=["food", "culture"] if index % 2 else ["nature"],
                days=1 if index % 2 == 0 else 2,
            )
            status, body, wall_ms = await _plan(client, payload)
            if status != 200:
                raise SystemExit(f"冷规划第 {index + 1} 次返回 {status}：{str(body)[:300]}")
            if (body.get("meta") or {}).get("cached") is True:
                raise SystemExit(
                    f"冷规划第 {index + 1} 次命中了缓存（meta.cached=true）—— 这不再是冷启动测量，"
                    "数字不能当门槛用。请确认参数基数与历史不同。"
                )
            cold.values.append(_server_ms(body, wall_ms))
            layer = str((body.get("meta") or {}).get("cache_layer"))
            cache_layers[layer] = cache_layers.get(layer, 0) + 1
        facts["cold_cache_layers"] = cache_layers

        # ── 缓存命中：同一份需求重复提交 ──
        same = _payload(
            free_text="想逛逛老城区，吃点本地小吃",
            budget={"amount": run_base + 1_000, "scope": "per_person"},
        )
        status, body, wall_ms = await _plan(client, same)
        if status != 200:
            raise SystemExit(f"缓存命中基准的第一次冷规划失败：{status} {str(body)[:300]}")
        trip_id = body["data"]["trip_id"]
        for _ in range(CACHE_HIT_SAMPLES - 1):
            status, body, wall_ms = await _plan(client, same)
            if status != 200:
                raise SystemExit(f"缓存命中重复请求失败：{status} {str(body)[:300]}")
            cache_hit.values.append(_server_ms(body, wall_ms))
        facts["cache_hit_meta"] = {
            "cached": (body.get("meta") or {}).get("cached"),
            "cache_layer": (body.get("meta") or {}).get("cache_layer"),
        }

        # ── 修改 ──
        instructions = [
            "改成 3 天",
            "预算改成 900 元",
            "走慢一点，少安排一点",
            "多加点吃的",
            "不要博物馆",
            "加一个看夜景的地方",
            "换成 4 个人",
            "把公园去掉",
            "中午要吃饭",
            "安排一次珠江夜游",
        ]
        for index in range(REVISE_SAMPLES):
            started = time.perf_counter()
            response = await client.post(
                f"/api/v1/trips/{trip_id}/revise",
                json={"instruction": instructions[index % len(instructions)]},
            )
            wall_ms = (time.perf_counter() - started) * 1000
            if response.status_code != 200:
                raise SystemExit(f"修改第 {index + 1} 次返回 {response.status_code}：{response.text[:300]}")
            # ★ 修改接口的响应 meta 里没有 elapsed_ms ★
            # 所以这里必须用客户端墙钟。曾经写成 `_server_ms(body, 0.0)`，
            # 结果 20 个样本全是 0ms 并且"达标" —— 一个恒为 0 的指标比没有指标更危险。
            revise.values.append(_server_ms(response.json(), wall_ms))

    samples.extend([cold, cache_hit, revise])

    # ── 含搜索（真 Key 才跑）──
    if not args.skip_search:
        from app.core.config import get_settings

        # ★ 先把 Provider 放回 auto 再判断能不能跑 ★
        # 本轮是关着搜索跑的（确定性），所以启动时设的是 seed_only；
        # 直接拿这个值判断"有没有搜索"会永远得到"没有"—— 自己把自己关掉了。
        # 真正的判据是：放开 auto 之后，`_effective` 能不能解析出一个真 Provider。
        _force_env(SEARCH_PROVIDER="auto")
        if get_settings().search_provider_effective in ("seed_only", "disabled"):
            search.note = "跳过：没有可用的搜索 Provider（需要 TAVILY_API_KEY）"
        else:
            from app.main import create_app as _create

            scenarios = [
                "想找广州最近新开的展览，几个人不多但有意思的地方",
                "2026 年广州有什么新开的餐厅值得去",
                "想看看最近有没有夜游珠江的新玩法",
            ]
            # ★ 必须核实"搜索真的发生了" ★
            # 规划链只驱动 L1/L3/L4/L7（`plan_service`），全仓没有任何地方请求
            # KIND_SEARCH —— 这三条"需搜索场景"根本不会联网。不核实就会把
            # 247ms 的"普通规划"当成"含搜索 p95"写进报告：一个看起来最漂亮、
            # 实际最假的数字。判据用产品自己的成本账（cost_logs.category='search'，
            # 与 /admin/cost 读的是同一份），而不是另搭一套埋点。
            before = await _count_search_calls()
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=_create()), base_url="http://perf.local"
            ) as client:
                for text in scenarios:
                    status, body, wall_ms = await _plan(
                        client,
                        _payload(
                            free_text=text,
                            budget={"amount": int(facts["run_seed"]) + 3_000, "scope": "per_person"},
                        ),
                    )
                    if status != 200:
                        search.note = f"跳过：搜索场景返回 {status}"
                        break
                    search.values.append(_server_ms(body, wall_ms))
                after = await _count_search_calls()
                if after == before:
                    search.skipped = True
                    observed = list(search.values)
                    search.values = []
                    search.target_ms = None
                    search.note = (
                        f"**不可测**：这 {SEARCH_SAMPLES} 条场景在规划链里一次搜索都没发生"
                        f"（cost_logs.category='search' 计数未变）。原因：L8 未接进规划链 —— "
                        "`plan_service` 只驱动 L1/L3/L4/L7，全仓没有任何地方请求 KIND_SEARCH"
                        "（TASKS.md 已记为刻意决定：接它等于让本地库不足时自动联网）。"
                        f"场景本身实测 {', '.join(f'{v:.0f}ms' for v in observed)}，"
                        "但那是普通规划，不能当『含搜索』用。搜索 Provider 自身的行为由 "
                        "`tests/integration/test_search_live.py` 覆盖。"
                    )
                else:
                    search.note = (
                        f"真实触发了 {after - before} 次搜索调用 · "
                        f"Provider：{get_settings().search_provider_effective}"
                    )
            _force_env(SEARCH_PROVIDER="seed_only")

    samples.append(search)
    return samples, facts


# ── 二、并发 20：不允许 5xx ─────────────────────────────────────────────────


async def _count_search_calls() -> int:
    """搜到几次联网搜索 —— 读产品自己的成本账，而不是我们另搭一套埋点。"""
    from sqlalchemy import func, select

    from app.db.models import CostLog
    from app.db.session import get_sessionmaker

    async with get_sessionmaker()() as session:
        return int(
            (
                await session.execute(
                    select(func.count()).select_from(CostLog).where(CostLog.category == "search")
                )
            ).scalar_one()
        )


async def measure_concurrency() -> tuple[Sample, dict[str, Any]]:
    import httpx

    from app.main import create_app

    # 同样要一轮一变的基数：20 条并发若命中历史缓存，就测不到并发写入（全部走复制路径）。
    concurrency_base = uuid.uuid4().int % 90_000 + 10_000
    sample = Sample(
        "concurrency_20",
        f"并发 {CONCURRENCY} 个冷规划",
        None,
        f"asyncio.gather({CONCURRENCY})，参数各不相同",
    )
    started = time.perf_counter()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()), base_url="http://perf.local"
    ) as client:
        responses = await asyncio.gather(
            *(
                client.post(
                    "/api/v1/trips:plan",
                    params={"sync": "true"},
                    json=_payload(
                        budget={"amount": concurrency_base + index, "scope": "per_person"}
                    ),
                )
                for index in range(CONCURRENCY)
            ),
            return_exceptions=True,
        )
    elapsed_ms = (time.perf_counter() - started) * 1000
    codes: list[int | str] = []
    for item in responses:
        if isinstance(item, BaseException):
            codes.append(type(item).__name__)
        else:
            codes.append(item.status_code)
    server_errors = [code for code in codes if isinstance(code, int) and code >= 500]
    assertions = {
        "codes": codes,
        "server_errors": server_errors,
        "wall_ms": round(elapsed_ms, 1),
        "ok": not server_errors,
    }
    sample.values.append(elapsed_ms)
    # 这条门槛的判据是"有没有 5xx / 异常异常名"，不是耗时，所以由这里显式给结论。
    sample.auto_ok = not server_errors
    sample.note = (
        f"全部 {CONCURRENCY} 条耗时 {elapsed_ms / 1000:.1f}s；5xx = {len(server_errors)}；"
        "注：in-process ASGI，不含网络与 uvicorn 开销"
    )
    return sample, assertions


# ── 三、束搜索 80 候选：纯计算 ──────────────────────────────────────────────


async def measure_beam_search() -> tuple[Sample, dict[str, Any]]:
    """按 ``limits.planning.candidate_max``（80）构造候选，跑**纯计算**的路线组合。

    刻意走领域层而不是 HTTP：这条门槛量的是算法本身，
    混进数据库与序列化就看不出"算法变慢了"还是"库变慢了"。
    """
    from sqlalchemy import select

    from app.core.config import get_limits_config, get_scoring_config
    from app.db.models import Place as PlaceRow
    from app.db.models import PlaceRelation
    from app.db.session import get_sessionmaker
    from app.domain.candidates import select_candidates
    from app.domain.models import RelationIndex
    from app.domain.planner import plan_routes
    from app.schemas.trips import PlanRequest
    from app.services.kb_layers import to_domain_place, to_domain_relation
    from app.services.plan_service import build_intent

    limits = get_limits_config()
    scoring = get_scoring_config()
    limit = limits.planning.candidate_max

    async with get_sessionmaker()() as session:
        city_id = (
            await session.execute(select(PlaceRow.city_id).where(PlaceRow.status == "active").limit(1))
        ).scalar_one()
        rows = (
            (
                await session.execute(
                    select(PlaceRow).where(PlaceRow.city_id == city_id, PlaceRow.status == "active")
                )
            )
            .scalars()
            .all()
        )
        places = [to_domain_place(row, scoring) for row in rows]

        payload = PlanRequest(city="guangzhou", days=1, people=2, preferences=["culture"], free_text="")
        intent, constraints, _parsed = build_intent(payload, limits=limits, scoring=scoring)
        candidate_set = select_candidates(places, intent, constraints, scoring=scoring, limits=limits)
        candidates = list(candidate_set.items)[:limit]

        ids = [candidate.place.id for candidate in candidates]
        relation_rows = (
            (
                await session.execute(
                    select(PlaceRelation).where(
                        PlaceRelation.place_a_id.in_(ids), PlaceRelation.place_b_id.in_(ids)
                    )
                )
            )
            .scalars()
            .all()
        )
        relations = RelationIndex.build([to_domain_relation(row) for row in relation_rows])

    sample = Sample(
        "beam_search_80",
        f"束搜索（{len(candidates)} 候选，三种 archetype）",
        BEAM_SEARCH_TARGET_MS,
        "领域层纯计算，不含数据库/序列化",
    )
    per_archetype: dict[str, float] = {}
    for archetype in ("relaxed", "classic", "themed"):
        started = time.perf_counter()
        plan_routes(
            candidates,
            intent,
            constraints,
            relations=relations,
            limits=limits,
            scoring=scoring,
            archetype=archetype,
        )
        per_archetype[archetype] = round((time.perf_counter() - started) * 1000, 2)
        sample.values.append(per_archetype[archetype])
    sample.note = (
        f"候选 {len(candidates)}（candidate_max={limit}）· 关系 {len(relation_rows)} 条 · "
        + "、".join(f"{k} {v}ms" for k, v in per_archetype.items())
    )
    return sample, {"per_archetype_ms": per_archetype, "candidates": len(candidates)}


# ── 四、慢查询：不许有 > 200ms 的语句 ───────────────────────────────────────


class _StatementTimer:
    """用 SQLAlchemy 事件统计每条语句的耗时。

    为什么不用 `pg_stat_statements`：它是**共享预加载**扩展，装上要改
    `postgresql.conf` 并重启集群。本机没装，而"为了测量去动别人的数据库配置"
    比换个测法糟得多。这里在应用侧计时，测的是同一条预算（有没有 > 200ms 的语句），
    并且能直接指认是哪条 SQL —— pg_stat_statements 还需要另行清洗指纹。
    """

    def __init__(self) -> None:
        self.slow: list[tuple[str, float]] = []
        self.count = 0
        self.total_ms = 0.0
        self.longest: list[float] = []
        #: 按阶段分开记账。混在一起会得出"有 61 条慢 SQL"这种无法行动的数字 ——
        #: 它们全部来自 20 并发的争用阶段，而 PRD 的"0 条 > 200ms"说的是正常负载。
        #: 分开之后：顺序阶段判门槛，并发阶段如实报告争用幅度。
        self.phases: dict[str, dict[str, Any]] = {}

    def snapshot(self, label: str) -> None:
        """结算当前阶段并清零，下一阶段重新计。"""
        self.phases[label] = {
            "statements": self.count,
            "total_ms": round(self.total_ms, 1),
            "max_ms": round(max(self.longest, default=0.0), 1),
            "over_200ms": len(self.slow),
            "slowest_sql": self.slow[0][0] if self.slow else None,
        }
        self.slow = []
        self.count = 0
        self.total_ms = 0.0
        self.longest = []

    def attach(self, engine: Any) -> None:
        from sqlalchemy import event

        # ★ 开始时刻写在 ExecutionContext 上，不用 id(cursor) 做键 ★
        # 用 `id(cursor)` 当键踩过一次：游标对象被回收后 id 会被复用，
        # 并发 20 条时残留的旧键会把别人更早的开始时刻借给当前语句，
        # 于是测出"单条 SQL 397ms、合计 64s"——而整轮墙钟只有十来秒。
        # 一个超过总时长的分项必然是测量错，不是数据库慢。
        @event.listens_for(engine.sync_engine, "before_cursor_execute")
        def _before(conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, many: bool) -> None:
            context.__dict__["_perf_started"] = time.perf_counter()

        @event.listens_for(engine.sync_engine, "after_cursor_execute")
        def _after(conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, many: bool) -> None:
            started = context.__dict__.pop("_perf_started", None)
            if started is None:
                return
            elapsed_ms = (time.perf_counter() - started) * 1000
            self.count += 1
            self.total_ms += elapsed_ms
            self.longest.append(elapsed_ms)
            if elapsed_ms > SLOW_STATEMENT_TARGET_MS:
                self.slow.append((" ".join(statement.split())[:160], round(elapsed_ms, 1)))


# ── 五、前端指标 ────────────────────────────────────────────────────────────


def parse_build_log(path: Path) -> dict[str, Any]:
    """从 `next build` 输出里取首屏 JS。

    ★ 取的是首页那一行的 **First Load JS**，两者不能相加 ★
    next 的表头是 `Route | Size | First Load JS`，其中 **First Load JS 已经是
    "页面自身 + shared" 的合计**（表下另列 shared 明细只是拆分）。第一版写成
    `shared + 首页`，于是一份 127 kB 的首屏被算成 229 kB 并"超檟"—— 一个只因
    重复计算而超标的门槛比没有门槛更坏，因为它会让人去优化不存在的问题。
    单位：next 打印的就是 **gzip 后**的大小。

    ★ 体积数字是"数字 + 单位"两个 token，不是 `9.4kB` ★
    第一版按 `token.endswith("kB")` 找体积，命中的永远是那个光秃秃的 `kB`，
    `float("kB"[:-2])` 抛 ValueError 后返回 None —— 于是首页与 shared 两项
    **恒为 None**，"首屏 JS ≤ 200KB"这一行从来没被算出来过（也从来没有
    因它而失败）。数字与单位之间还可能是普通的空格，用正则一次拿准。
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    shared = None
    for line in text.splitlines():
        if "First Load JS shared by all" in line:
            shared = _first_kb(line)
    pages: dict[str, float] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith(("└", "├", "┌")):
            continue
        # 形如 `┌ ○ /    9.4 kB    127 kB`：去掉树形符号与路由类型标记（○/ƒ/λ）
        body = stripped.lstrip("└├┌─ ").lstrip("○ƒλ ").strip()
        if not body.startswith("/"):
            continue
        route = body.split()[0]
        size = _last_kb(stripped)
        if size is not None:
            pages[route] = size
    return {"shared_kb": shared, "pages_kb": pages, "home_kb": pages.get("/")}


#: `next build` 的体积写法：数字与单位之间可能有空格（`9.4 kB`）。
_KB_RE = re.compile(r"(\d+(?:\.\d+)?)\s*kB")


def _first_kb(line: str) -> float | None:
    match = _KB_RE.search(line)
    return float(match.group(1)) if match else None


def _last_kb(line: str) -> float | None:
    """取这一行**最后一个**体积 —— 路由行里 `Size` 在前、`First Load JS` 在后。"""
    matches = _KB_RE.findall(line)
    return float(matches[-1]) if matches else None


# ── 报告 ────────────────────────────────────────────────────────────────────


def _fmt_ms(value: float | None) -> str:
    return "—" if value is None else f"{value:.0f} ms"


def _fmt_value(value: float | None, key: str) -> str:
    """按指标本身的单位打印。

    首屏 JS 的样本值是**字节**，混在毫秒里会打印出 "最小 130048 ms" ——
    一个把 127 kB 说成 130048 毫秒的报告，读者只会把它当成又一个看不懂的数字。
    拿不到就写 "—"，不用 0 顶替（"null ≠ 0"在这里同样适用）。
    """
    if value is None:
        return "—"
    return f"{value / 1024:.1f} KB" if key == "first_load_js" else f"{value:.0f} ms"


def render_report(
    samples: Iterable[Sample],
    facts: dict[str, Any],
    extra: dict[str, Any],
    *,
    generated_at: str,
) -> str:
    lines = [
        "# 性能报告（PRD §23.6）",
        "",
        f"- 生成时间：{generated_at}",
        "- 生成方式：`make perf`（后端 `scripts/benchmark.py` + 前端 `next build` 产物）",
        "- 口径：分位数用最近秩（rank = ceil(q·n)），**样本量一并列出来了** ——",
        "  n=10 的 p95 就是最大的那个样本，n=20 的 p95 是第二大的那个；样本这么少时",
        "  不要把它当统计学结论读。耗时优先取响应里的 `meta.elapsed_ms`（后端计时）。",
        "- 「结论」一列里 **⏭ 未测 / 不可测 不是达标**：没测到的门槛一律显式写出来，",
        "  而不是留空或打勾（本项曾经把没测过的 TTI 与没设阈值的并发都渲染成 ✅）。",
        "",
        "## 门槛一览",
        "",
        "| 指标 | 目标 | 方法 | 实测 | 结论 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for sample in samples:
        target = "—" if sample.target_ms is None else f"≤ {sample.target_ms / 1000:.1f}s".replace(".0s", "s")
        if sample.target_ms is not None and sample.target_ms < 1000:
            target = f"≤ {sample.target_ms}ms"
        if sample.key == "beam_search_80":
            target = f"< {sample.target_ms}ms"
        if sample.key == "concurrency_20":
            target = "无 5xx / 无死锁"
        if sample.key == "first_load_js":
            target = "≤ 200KB gzip"
        measured = _fmt_value(sample.values[0] if sample.values else None, sample.key)
        if sample.skipped:
            verdict = "⏭ 不可测"
        elif not sample.values:
            verdict = "⏭ 未测"
        elif sample.ok is None:
            # 没有 threshold、测量方也没给结论 —— 如实写"没结论"。
            verdict = "—（无门槛）"
        else:
            verdict = "✅" if sample.ok else "❌"
        lines.append(f"| {sample.label} | {target} | {sample.method} | {measured} | {verdict} |")

    lines += ["", "## 明细", ""]
    for sample in samples:
        lines.append(f"### {sample.label}")
        lines.append("")
        lines.append(f"- 方法：{sample.method}")
        if sample.values:
            lines.append(
                f"- 样本 n={len(sample.values)} · 最小 {_fmt_value(min(sample.values), sample.key)} · "
                f"中位 {_fmt_value(sample.median, sample.key)} · "
                f"**p95 {_fmt_value(sample.p95, sample.key)}** · "
                f"最大 {_fmt_value(max(sample.values), sample.key)}"
            )
        elif sample.skipped:
            lines.append("- 样本 n=0（该项无法有效测量，理由见下）")
        if sample.note:
            lines.append(f"- 说明：{sample.note}")
        lines.append("")

    if "cold_cache_layers" in facts:
        lines.append(
            "- 冷规划的缓存层分布："
            + "、".join(f"{k}×{v}" for k, v in facts["cold_cache_layers"].items())
        )
    if "cache_hit_meta" in facts:
        lines.append(f"- 缓存命中的响应元信息：`{facts['cache_hit_meta']}`")

    if extra:
        lines += ["", "## 其他核对项", ""]
        for key, value in extra.items():
            lines.append(f"- **{key}**：{value}")
    lines.append("")
    return "\n".join(lines)


# ── 入口 ────────────────────────────────────────────────────────────────────


async def _run(
    args: argparse.Namespace, timer: _StatementTimer
) -> tuple[list[Sample], dict[str, Any], dict[str, Any]]:
    """先把历史限流计数清掉，再开测（否则昨天的配额会把今天的第一批打成 429）。"""
    await _clear_rate_counters()
    return await _measure_all(args, timer)


async def _measure_all(args: argparse.Namespace, timer: _StatementTimer) -> tuple[list[Sample], dict[str, Any], dict[str, Any]]:
    """所有阶段跑在**同一个事件循环**里。

    ★ 为什么要这样 ★
    进程内的引擎缓存（``db/session.py`` 的 ``_engines``）会把连接绑在创建它的
    事件循环上；每换一个 ``asyncio.run`` 就是换一个循环，第二段就会撞上
    ``got Future attached to a different loop``（M1 的 ``/health`` 就是踩了这个坑）。
    全放在一个循环里既避开了它，也顺带让 SQL 计时覆盖完整的一轮压测。
    """
    samples, facts = await measure_backend(args)
    timer.snapshot("顺序阶段（冷规划/缓存命中/修改）")
    concurrency_sample, assertions = await measure_concurrency()
    timer.snapshot("并发 20 阶段（SQL 争用）")
    beam_sample, beam_facts = await measure_beam_search()
    timer.snapshot("束搜索阶段（纯计算，仅取数）")
    samples.extend([concurrency_sample, beam_sample])

    # 开 LLM 的观察值：不计入门槛，只回答"慢在哪"。
    llm_observation: dict[str, float] = {}
    _force_env(LLM_PROVIDER="deepseek")
    from app.core.config import get_settings

    # 与冷规划同一套基数偏移（+2000 段），同样是为了不撞历史缓存。
    llm_base = int(facts.get("run_seed", 0)) + 2_000
    if get_settings().llm_provider_effective == "deepseek":
        import httpx

        from app.main import create_app

        values: list[float] = []
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app()), base_url="http://perf.local"
        ) as client:
            for index in range(3):
                status, body, wall_ms = await _plan(
                    client,
                    _payload(budget={"amount": llm_base + index * 31, "scope": "per_person"}),
                )
                if status == 200:
                    values.append(_server_ms(body, wall_ms))
        if values:
            llm_observation = {
                "n": float(len(values)),
                "median_ms": percentile_nearest_rank(values, 0.5),
                "max_ms": max(values),
            }
    _force_env(LLM_PROVIDER="disabled")
    return samples, facts, {
        "llm": llm_observation,
        "assertions": assertions,
        "beam": beam_facts,
        "sql_phases": timer.phases,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="性能压测与门槛核对")
    parser.add_argument("--skip-search", action="store_true", help="跳过含搜索的场景（不花外部调用）")
    parser.add_argument("--frontend-build-log", type=Path, default=None, help="next build 的输出日志")
    parser.add_argument("--web-vitals", type=Path, default=None, help="web-vitals.mjs 输出的 LCP JSON")
    parser.add_argument("--out", type=Path, default=None, help="报告输出路径")
    parser.add_argument("--json", type=Path, default=None, help="追加输出机器可读结果")
    args = parser.parse_args(argv)

    # 压测的 Provider 组合必须确定：LLM 关掉（另记一条观察值）、搜索关掉（除非测含搜索）。
    _force_env(LLM_PROVIDER="disabled", SEARCH_PROVIDER="seed_only", FAULT_INJECTION="")
    _raise_rate_limits()

    timer = _StatementTimer()
    from app.db.session import get_engine

    timer.attach(get_engine())

    samples, facts, results = asyncio.run(_run(args, timer))
    assertions = results["assertions"]
    beam_facts = results["beam"]
    llm_observation: dict[str, float] = results["llm"]
    sql_phases: dict[str, dict[str, Any]] = results["sql_phases"]

    extra: dict[str, Any] = {}
    if llm_observation:
        extra["开着 LLM 的冷规划（仅观察，不计入门槛）"] = (
            f"n={int(llm_observation['n'])} · 中位 {llm_observation['median_ms']:.0f} ms · "
            f"最大 {llm_observation['max_ms']:.0f} ms（门槛用的是关掉 LLM 的规则引擎路径）"
        )
    sequential = [
        stats
        for label, stats in sql_phases.items()
        if not label.startswith("并发")
    ]
    slow_sample = Sample(
        "db_slow_statement",
        "最慢的单条 SQL（PRD：0 条 > 200ms）",
        SLOW_STATEMENT_TARGET_MS,
        "SQLAlchemy 事件计时（本机没装 pg_stat_statements 扩展）",
    )
    slow_sample.values.append(max((s["max_ms"] for s in sequential), default=0.0))
    seq_statements = sum(int(s["statements"]) for s in sequential)
    seq_total = sum(float(s["total_ms"]) for s in sequential)
    seq_over = sum(int(s["over_200ms"]) for s in sequential)
    slow_sample.note = (
        f"顺序阶段：{seq_statements} 条语句 · 合计 {seq_total:.0f} ms · 超过 200ms 的 {seq_over} 条 · "
        f"最慢一条 {max((s['max_ms'] for s in sequential), default=0.0):.0f} ms"
        + (f"（{sequential[0]['slowest_sql'][:80]}…）" if seq_over and sequential[0].get("slowest_sql") else "")
        + "。并发阶段的争用单独列在下面，不计入这条门槛"
    )
    samples.append(slow_sample)
    for label, stats in sql_phases.items():
        extra[f"SQL 统计 · {label}"] = (
            f"{stats['statements']} 条 · 合计 {stats['total_ms']} ms · "
            f"最慢 {stats['max_ms']} ms · 超 200ms {stats['over_200ms']} 条"
        )
    extra[f"并发 {CONCURRENCY} 的响应码"] = f"{assertions['codes']} · 墙钟 {assertions['wall_ms']} ms"
    extra["束搜索候选数"] = f"{beam_facts.get('candidates')}"
    extra["测量限制"] = (
        "in-process ASGI（不含网络/uvicorn）；本机为开发机，数字用于回归对比而不是容量规划"
    )

    if args.web_vitals and args.web_vitals.exists():
        vitals = json.loads(args.web_vitals.read_text(encoding="utf-8"))
        pages = vitals.get("pages", {})
        share = pages.get("share") or {}
        home = pages.get("home") or {}
        if share.get("lcp_median_ms") is not None:
            share_sample = Sample(
                "share_lcp",
                "分享页 LCP",
                SHARE_LCP_TARGET_MS,
                "Playwright + CDP 节流（4G、CPU 4x、移动端 viewport），取 3 次中位数",
            )
            share_sample.values.append(float(share["lcp_median_ms"]))
            share_sample.note = (
                f"样本 {share.get('lcp_samples_ms')} ms · 页面 {share.get('url')}。"
                "用 CDP 而不是 Lighthouse CLI：仓库已有 Playwright 与 Chrome，"
                "不为一行指标再装第二个浏览器；代价是没有 Lighthouse 的可访问性/SEO 审计。"
            )
            samples.append(share_sample)
        if home.get("lcp_median_ms") is not None:
            # ★ 这一行刻意不做成门槛行 ★
            # PRD 要的是首页 TTI ≤ 3s。把 LCP 读到 2720ms 再拿 3000ms 去比，会打印出一个
            # "✅ 首页达标" —— 而 TTI 根本没测。那是一个谎报。所以：门槛行写"未测"，
            # LCP 读数放在观察项里，并写明 TTI ≥ LCP 这条恒等式意味着什么。
            tti = Sample(
                "home_tti",
                "首页 TTI",
                HOME_TTI_TARGET_MS,
                "未测（需长任务分析）",
                skipped=True,
            )
            tti.note = (
                "**未测**：TTI 的正确定义是「首个 5s 静默窗之前最后一个长任务结束」，"
                "需要长任务分析，本轮没做 —— 宁可不写，也不拿 domInteractive 之类的东西顶替。"
            )
            samples.append(tti)
            # ★ 不写死数字，也不替 TTI 下结论 ★
            # 上一版这里写死了一句"首页 LCP 已 2.7s 说明 TTI 多半越过 3s"：
            # 那个 2.7s 是第一次运行时的读数，之后每次重跑都带着它（没人发现，
            # 因为句子读起来很顺）；而 TTI ≥ LCP 只给出**下界**，从中推不出
            # “TTI 达标”也推不出“越线”—— 只能如实说它是个下界。
            extra["首页 LCP（观察值，不是 TTI）"] = (
                f"样本 {home.get('lcp_samples_ms')} ms · 中位 {home['lcp_median_ms']} ms。"
                "因 TTI ≥ LCP 恒成立，这个读数就是 **TTI 的下界**：LCP 越接近 3s 门槛，"
                "TTI 越需要单独确认（要长任务分析）。首屏瓶颈是 Hero 视频。"
            )
        if share.get("lcp_median_ms") is None:
            # 没采到就说没采到。分享页 LCP 是 PRD 明写的一项门槛，
            # 缺了它必须能被看见（而不是从表里默默消失）。
            extra["分享页 LCP"] = (
                "未测：web-vitals 的输出里没有 `share` 一项。采集脚本 `frontend/scripts/perf-lcp.sh` "
                "会造一条真实的分享行程拿 slug；造不出来（后端没起、限流、没有知识库）时它不编造化测。"
            )
    elif args.web_vitals:
        extra["LCP"] = f"未测（{args.web_vitals} 不存在；先跑 frontend/scripts/web-vitals.mjs）"

    if args.frontend_build_log:
        build = parse_build_log(args.frontend_build_log)
        if build["home_kb"] is not None:
            bundle = Sample(
                "first_load_js",
                "首屏 JS（首页 First Load JS）",
                FEATURE_JS_TARGET_BYTES,
                "`next build` 输出的 gzip 后体积",
            )
            bundle.values.append(build["home_kb"] * 1024)
            bundle.note = (
                f"首页 First Load JS {build['home_kb']} kB（其中 shared {build['shared_kb']} kB，"
                "已含在内，不重复相加）；各页面 First Load JS 见下表。单位均为 gzip 后"
            )
            samples.append(bundle)
            extra["各页面 First Load JS（kB）"] = ", ".join(
                f"{k} {v}" for k, v in sorted(build["pages_kb"].items())[:12]
            )

    out_path = args.out or (Path(__file__).resolve().parent.parent.parent / "docs" / "PERF_REPORT.md")
    report = render_report(
        samples, facts, extra, generated_at=time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    )
    out_path.write_text(report, encoding="utf-8")
    print(report)
    print(f"\n报告写入：{out_path}")

    if args.json:
        args.json.write_text(
            json.dumps(
                {
                    "samples": [
                        {
                            "key": s.key,
                            "label": s.label,
                            "target_ms": s.target_ms,
                            "n": len(s.values),
                            "p95": s.p95,
                            "median": s.median,
                            "max": max(s.values) if s.values else None,
                            "ok": s.ok,
                            "note": s.note,
                        }
                        for s in samples
                    ],
                    "facts": facts,
                    "slow_statements": timer.slow,
                },
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )

    failed = [s.label for s in samples if s.ok is False and s.values]
    unmeasured = [s.label for s in samples if s.skipped]
    if failed:
        print(f"\n❌ 未达门槛：{'、'.join(failed)}")
        return 1
    tail = f"（另有 {len(unmeasured)} 项无法有效测量：{'、'.join(unmeasured)}）" if unmeasured else ""
    print(f"\n✅ 已测的门槛全部达标{tail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
