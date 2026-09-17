"""联网搜索管线（PRD §13.2 查询生成 → §13.3 处理管线 → §13.4 触发决策树）。

★ 这个模块存在的唯一理由 ★
搜索是**唯一一个"每次调用都要花钱、而不搜也不会错"**的外部依赖。所以它的正确性
不在"能不能搜到"，而在 **"什么时候不该搜"**：决策树写在 :func:`decide_triggers`，
每条分支都带自己的预算与触发理由，理由会原样出现在用户的 ``degraded_modes`` 里 ——
用户有权知道这次为什么联网了。

★ 三级护栏（都是 PRD 的硬要求）★
1. **决策树**：只有 §13.4 列举的五种情况才触发（:data:`TriggerKind`）。默认路径
   （本地库够用、用户没要求最新）**一次搜索都不发**，即 ≈95% 的请求搜索成本为 0。
2. **预算**：每种触发的查询数上限来自 ``config/ttl.yaml`` 的 ``refresh_policy.search_budget``，
   全局硬上限来自 ``limits.providers.search_max_queries_per_plan``（8）；金额熔断由
   ``CostLedger`` 按 ``limits.cost.circuit_breaker.plan_search_cny`` 兜住。
3. **修改不搜索**：PRD §13.2 明令"为路线微调触发搜索 → 调用数必须为 0"。
   调用方（``plan_service``）在 ``parent_trip is not None`` 时**根本不构造**本管线。

★ 抽取结果只做两件事，绝不改行程事实 ★
- 与库内一致 → 提升 ``confidence`` 并登记字段级来源（```place_sources``）；
- 与库内冲突 → 标 ``verification_status='conflicting'`` + 数据质量标记，把双方来源都留着；
- 库里没有 → 作为**线索**登记进 ``travel_sources.extracted_facts``。

关于"新地点入候选池（``status='candidate'``）"（PRD §13.3）：``places`` 表的
``latitude/longitude`` 是 NOT NULL 且禁止 0 值，而搜索片段里没有可信坐标 ——
给它编一个坐标就是把幻觉写进知识库。因此本实现把库外新地点登记为**线索**
（返回给调用方并在降级说明里如实写出"缺坐标"），等有人工/官方来源补上坐标后
再入池。这条差异是刻意的，写在 ``SearchEvidence.leads_note()`` 里而不是藏起来。
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import LimitsConfig, ScoringConfig, TtlConfig
from app.core.logging import get_logger
from app.db.models import Place, PlaceAlias, PlaceSource, TravelSource
from app.domain.models import Intent
from app.domain.models import Place as DomainPlace
from app.domain.naming import normalize_name, similarity
from app.providers.search.base import SearchResult
from app.services.cache import CacheStore, search_cache_key, sha256_hex
from app.services.cost import CostLedger
from app.services.llm_planner import (
    SEARCH_FACT_FIELDS,
    LlmPlanner,
    LlmSearchObservation,
)
from app.services.retrieval_chain import RetrievalChain, RetrievalQuery
from app.services.retrieval_layers import KIND_SEARCH

__all__ = [
    "SearchEvidence",
    "SearchPipeline",
    "SearchTrigger",
    "TriggerKind",
    "decide_triggers",
    "preference_queries",
]

log = get_logger("search")

TriggerKind = Literal[
    "verify_hard_constraint",
    "user_named_unknown_place",
    "discover_more_candidates",
    "user_requested_latest",
    "arbitrate_conflict",
]

#: 触发类型 → 面向用户的中文理由（进 degraded 说明，不是日志用词）
TRIGGER_LABELS: dict[TriggerKind, str] = {
    "verify_hard_constraint": "库内营业时间已过期且出行临近",
    "user_named_unknown_place": "提到了库里没有的地点",
    "discover_more_candidates": "符合条件的地点太少",
    "user_requested_latest": "要求最新/近期信息",
    "arbitrate_conflict": "库内数据存在来源冲突",
}

#: 触发类型 → ``refresh_policy.search_budget`` 里的预算键
_BUDGET_KEYS: dict[TriggerKind, str] = {
    "verify_hard_constraint": "verify_hard_constraint",
    "user_named_unknown_place": "user_named_unknown_place",
    "discover_more_candidates": "discover_more_candidates",
    "user_requested_latest": "user_requested_latest",
    "arbitrate_conflict": "arbitrate_conflict",
}

#: 偏好维度 → 查询关键词（PRD §13.2 的"偏好查询"，键与 scoring.yaml 的
#: ``preference_dimensions`` 对齐）。没列出的维度不生成查询 —— 宁可少搜。
_PREFERENCE_KEYWORDS: dict[str, str] = {
    "food": "美食 推荐",
    "photo": "拍照 机位",
    "culture": "文化 展览",
    "night_view": "夜景",
    "family": "亲子",
    "couple": "情侣 约会",
    "citywalk": "citywalk 路线",
    "nature": "公园 自然",
    "shopping": "购物 商圈",
    "museum": "博物馆 展览",
}

#: 冲突标记（写进 ``places.data_quality_flags``）。带 ``search_`` 前缀以便与
#: 建库质检脚本写下的标记区分开：来源不同，处理方式也不同。
_CONFLICT_FLAG = "search_conflict"

#: 与库内事实"算不算同一个说法"的归一化：只去空白与常见全角标点差异。
#: **刻意不做语义比较** —— "周一闭馆" vs "每周一不开放" 是同一个意思，
#: 但判断这件事需要语义理解，而误判的代价（把一致写成冲突）比漏判更大：
#: 冲突会直接改动 ``verification_status``，漏判只是少涨一次 confidence。
_STRIP_CHARS = str.maketrans({"　": "", " ": "", "：": ":", "，": ",", "。": ".", "～": "~", "—": "-"})


@dataclass(frozen=True, slots=True)
class SearchTrigger:
    """一次触发：类型 + 理由 + 本次要发的查询（已按预算裁剪）。"""

    kind: TriggerKind
    queries: tuple[str, ...]

    @property
    def label(self) -> str:
        return TRIGGER_LABELS[self.kind]


@dataclass
class SearchEvidence:
    """一次规划里搜索**实际发生了什么**（响应 meta / 降级说明 / 报表共用这一份事实）。

    每一格都可以为 0/空 —— 那正是常态（默认路径一次都不搜）。
    """

    triggers: tuple[SearchTrigger, ...] = ()
    queries: tuple[str, ...] = ()
    provider: str = ""
    calls: int = 0
    cache_hits: int = 0
    result_count: int = 0
    sources_written: int = 0
    confirmed: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    leads: tuple[str, ...] = ()
    cost_cny: Decimal = Decimal("0")
    notes: list[str] = field(default_factory=list)

    @property
    def searched(self) -> bool:
        return self.calls + self.cache_hits > 0

    def summary(self) -> dict[str, Any]:
        """给 ``plan.progress`` 事件用的紧凑摘要（前端据此显示"这次联网了"）。"""
        if not self.triggers and not self.searched:
            return {"used": False}
        return {
            "used": self.searched,
            "provider": self.provider or None,
            "triggers": [trigger.kind for trigger in self.triggers],
            "queries": len(self.queries),
            "calls": self.calls,
            "cache_hits": self.cache_hits,
            "results": self.result_count,
            "confirmed": len(self.confirmed),
            "conflicts": len(self.conflicts),
            "leads": len(self.leads),
        }


# ════════════════════════════════════════════════════════════════════════════
# 一、触发决策（PRD §13.4）与查询生成（PRD §13.2）—— 纯函数，可脱离 IO 单测
# ════════════════════════════════════════════════════════════════════════════


def preference_queries(intent: Intent) -> tuple[str, ...]:
    """按用户偏好生成查询关键词（PRD §13.2 最多 3 条）。城市名由调用方拼。"""
    keywords: list[str] = []
    for name in intent.active_preferences:
        keyword = _PREFERENCE_KEYWORDS.get(name)
        if keyword and keyword not in keywords:
            keywords.append(keyword)
    return tuple(keywords[:3])


@dataclass(frozen=True, slots=True)
class _Candidate:
    """决策树需要的、关于某个候选地点的最小事实集。"""

    name: str
    has_hours: bool
    hours_stale: bool
    conflicting: bool


def decide_triggers(
    *,
    city_name: str,
    intent: Intent,
    free_text: str,
    unknown_names: Sequence[str],
    candidate_count: int,
    candidates: Sequence[_Candidate],
    limits: LimitsConfig,
    ttl: TtlConfig,
    budget_ready: bool,
) -> tuple[SearchTrigger, ...]:
    """PRD §13.4 决策树。**顺序即优先级**，查询总量再在 :meth:`plan_queries` 里裁。

    ``budget_ready`` 为 False 时只产出"不该搜"的判断结果（返回空元组），
    由调用方用 ``unavailable_note()`` 说明原因 —— 省得每个分支都判一次。
    """
    if not budget_ready:
        return ()

    days = max(1, intent.days)
    policy = limits.search
    budget = _search_budget(ttl)
    triggers: list[SearchTrigger] = []

    def take(kind: TriggerKind, queries: list[str]) -> None:
        cap = budget.get(kind, 0)
        if cap <= 0:
            return
        cleaned: list[str] = []
        for query in queries:
            text = " ".join(query.split())
            if text and text not in cleaned:
                cleaned.append(text)
        if cleaned:
            triggers.append(SearchTrigger(kind=kind, queries=tuple(cleaned[:cap])))

    # 1. 数据明显冲突 → 搜索仲裁（≤1 次）。最先做：冲突会影响校验结论。
    conflicting = [item for item in candidates if item.conflicting]
    if conflicting:
        take(
            "arbitrate_conflict",
            [f"{item.name} 最新 公告 通知" for item in conflicting],
        )

    # 2. 硬约束字段过期 + 出行临近 → 复验（≤2 次）
    stale = [item for item in candidates if item.has_hours and item.hours_stale]
    if stale:
        take("verify_hard_constraint", [f"{item.name} 开放时间" for item in stale])

    # 3. 用户点名了库外地点 → 实体查询（≤2 次）
    if unknown_names:
        take(
            "user_named_unknown_place",
            [f"{name} 开放时间 门票" for name in unknown_names],
        )

    # 4. 候选不足 → 发现新地点（≤3 次）。锚定查询在这里出（§13.2 的"必出"）。
    if candidate_count < policy.discovery_candidate_below:
        anchor = [f"{city_name} {days}日游 路线"]
        anchor.extend(f"{city_name} {keyword}" for keyword in preference_queries(intent))
        take("discover_more_candidates", anchor)

    # 5. 用户明确要求"最新/近期/实时" → 时效查询（≤3 次）
    if policy.asks_for_latest(free_text):
        # 顺序有讲究：**时效查询紧跟锚定查询**。这一条分支的独有价值就在"最新的
        # 活动/展览"上，若把它排在偏好查询后面，3 条的预算会被偏好查询吃光，
        # 用户明确要的"最新"反而一条都没搜（实测就是这样发现的）。
        latest = [f"{city_name} {days}日游 路线", f"{city_name} {_month_label(intent)} 活动 展览"]
        latest.extend(f"{city_name} {keyword}" for keyword in preference_queries(intent))
        take("user_requested_latest", latest)

    return tuple(triggers)


def plan_queries(triggers: Sequence[SearchTrigger], *, hard_cap: int) -> tuple[str, ...]:
    """把各触发的查询拼起来（按优先级），并施加全局硬上限（PRD §13.2 的 ≤8）。"""
    seen: list[str] = []
    for trigger in triggers:
        for query in trigger.queries:
            if query not in seen:
                seen.append(query)
    return tuple(seen[: max(0, hard_cap)])


def _month_label(intent: Intent) -> str:
    """时效查询里的月份：优先用出行日期，没填就用今天（"最近"是按当下理解的）。"""
    raw = intent.travel_date
    if raw:
        try:
            parsed = date.fromisoformat(raw)
        except ValueError:
            parsed = None
        if parsed is not None:
            return f"{parsed.month}月"
    return f"{datetime.now(UTC).date().month}月"


def _search_budget(ttl: TtlConfig) -> dict[TriggerKind, int]:
    """从 ``refresh_policy.search_budget`` 读各触发的查询预算（脏值按 0 处理）。"""
    raw = ttl.refresh_policy.get("search_budget")
    if not isinstance(raw, Mapping):
        return {}
    budget: dict[TriggerKind, int] = {}
    for kind, key in _BUDGET_KEYS.items():
        value = raw.get(key)
        budget[kind] = _as_int(value, 0)
    return budget


def _as_int(value: Any, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return default
    try:
        number = int(value)
    except ValueError:
        return default
    return max(0, number)


# ════════════════════════════════════════════════════════════════════════════
# 二、管线
# ════════════════════════════════════════════════════════════════════════════


@dataclass
class SearchPipeline:
    """一次规划内的搜索：决策 → 查询 → 抽取 → 与库内比对 → 落库。"""

    chain: RetrievalChain
    db: AsyncSession
    planner: LlmPlanner
    ledger: CostLedger
    scoring: ScoringConfig
    limits: LimitsConfig
    ttl: TtlConfig
    provider_name: str
    provider_available: bool = True

    async def run(
        self,
        *,
        city_name: str,
        intent: Intent,
        free_text: str,
        unparsed: Sequence[str],
        candidates: Sequence[DomainPlace],
        today: date | None = None,
    ) -> SearchEvidence:
        """跑完一次搜索管线。**任何失败都不抛异常**（搜索是可选增强，不是主链路）。"""
        evidence = SearchEvidence(provider=self.provider_name)
        rows = await self._load_rows([candidate.id for candidate in candidates])
        date_is_close = self._travel_date_is_close(intent, today=today)
        unknown_names = self._unknown_names(unparsed, candidates)

        triggers = decide_triggers(
            city_name=city_name,
            intent=intent,
            free_text=free_text,
            unknown_names=unknown_names,
            candidate_count=len(candidates),
            candidates=[self._candidate_facts(row, date_is_close=date_is_close) for row in rows],
            limits=self.limits,
            ttl=self.ttl,
            budget_ready=self.provider_available,
        )
        evidence.triggers = triggers
        if not triggers:
            if not self.provider_available:
                evidence.notes.append(self.unavailable_note())
            return evidence

        queries = plan_queries(
            triggers, hard_cap=self.limits.providers.search_max_queries_per_plan
        )
        evidence.queries = queries
        if not self.provider_available:
            evidence.notes.append(self.unavailable_note())
            return evidence

        results = await self._collect(queries, evidence)
        if not results:
            evidence.notes.append("search:no_result(已联网查询但没有拿到可用结果)")
            return evidence

        evidence.result_count = len(results)
        observations = self._validated_observations(
            await self.planner.extract_search(results, question=queries[0]), results
        )
        if not observations:
            evidence.notes.append(
                "search:no_fact_extracted(搜索结果只登记为来源，未抽到可核验的事实)"
            )

        await self._apply(
            observations=observations,
            results=results,
            rows=rows,
            candidates=candidates,
            queries=queries,
            evidence=evidence,
        )
        return evidence

    def unavailable_note(self) -> str:
        return (
            "search:unavailable(没有可用的搜索 Provider，本次未联网；"
            "命中触发条件但按降级矩阵跳过，信息基于本地知识库)"
        )

    # ── 查询与结果 ──

    async def _collect(
        self, queries: Sequence[str], evidence: SearchEvidence
    ) -> tuple[SearchResult, ...]:
        """逐条查询并收集结果。熔断/失败即**停止继续查**（已经花的钱不再追加）。"""
        store = CacheStore(self.db)
        collected: dict[str, SearchResult] = {}
        for query in queries:
            cache_key = search_cache_key(
                provider=self.provider_name,
                query=query,
                locale="zh-CN",
                max_results=self.limits.providers.search_max_results_per_query,
            )
            try:
                outcome = await self.chain.resolve(
                    RetrievalQuery(
                        kind=KIND_SEARCH,
                        key=cache_key,
                        payload={
                            "cache_key": cache_key,
                            "query": query,
                            "locale": "zh-CN",
                            "max_results": self.limits.providers.search_max_results_per_query,
                        },
                    )
                )
            except Exception:  # pragma: no cover - 链只兜 ProviderError/CostBreakerOpen
                log.warning("搜索查询抛出意外异常，跳过该条", exc_info=True)
                evidence.notes.append("search:error(联网查询失败，已继续用本地数据出方案)")
                break
            if not outcome.resolved:
                # 熔断或 Provider 失败：继续查下去只会继续花钱/继续失败。
                evidence.notes.append(
                    "search:stopped(查询被熔断或 Provider 不可用，已停止后续搜索)"
                )
                break

            items = _as_results(outcome.value)
            cached = isinstance(outcome.value, Mapping)
            if cached:
                evidence.cache_hits += 1
                self.ledger.record_cache_hit("search", self.provider_name, operation=query)
            else:
                evidence.calls += 1
                await self._cache_results(store, cache_key=cache_key, query=query, items=items)
            for item in items:
                if item.url not in collected:
                    collected[item.url] = item
        evidence.cost_cny = self.ledger.spent_cny("search")
        return tuple(collected.values())

    async def _cache_results(
        self,
        store: CacheStore,
        *,
        cache_key: str,
        query: str,
        items: Sequence[SearchResult],
    ) -> None:
        """把搜索结果写进 ``search_cache``（TTL: ``search_result``）。

        写失败**不影响本次结果**：缓存只是让下次不花钱，不是这条信息的依据。
        """
        ttl_hours = self.ttl.ttl_for("search_result")
        if ttl_hours is None or not items:
            return
        try:
            await store.put_search(
                cache_key=cache_key,
                provider=self.provider_name,
                query=query,
                result={"query": query, "results": [_result_to_dict(item) for item in items]},
                ttl_hours=ttl_hours,
                locale="zh-CN",
            )
        except Exception:  # pragma: no cover - 同 LLM 缓存：坏了只是下次再花钱
            log.warning("写入搜索缓存失败（不影响本次结果）", exc_info=True)

    # ── §13.3 处理管线 ──

    async def _apply(
        self,
        *,
        observations: Sequence[LlmSearchObservation],
        results: Sequence[SearchResult],
        rows: Sequence[Place],
        candidates: Sequence[DomainPlace],
        queries: Sequence[str],
        evidence: SearchEvidence,
    ) -> None:
        by_url = {item.url: item for item in results}
        index = self._name_index(rows)
        aliases = await self._alias_index(rows)
        sources, created = await self._ensure_sources(results, queries, evidence)
        existing_links = await self._existing_links([row.id for row in rows])
        observations_by_url: dict[str, list[LlmSearchObservation]] = {}
        for item in observations:
            source_row = sources.get(sha256_hex(item.url))
            if source_row is None:
                continue  # URL 不在本次结果里：模型编的或改写过，直接丢
            observations_by_url.setdefault(item.url, []).append(item)

        confirmed: list[str] = []
        conflicts: list[str] = []
        leads: list[str] = []
        now = datetime.now(UTC)
        for item in observations:
            source_row = sources.get(sha256_hex(item.url))
            if source_row is None:
                continue
            place = _match_place(
                item.place_name,
                index,
                aliases=aliases,
                floor=self.limits.search.entity_match_floor,
            )
            if place is None:
                if item.place_name not in leads:
                    leads.append(item.place_name)
                _remember_discovered(source_row, item, results_by_url=by_url)
                continue
            key = (place.id, source_row.id)
            if key not in existing_links:
                self.db.add(
                    PlaceSource(place_id=place.id, source_id=source_row.id, field_scope=[item.field])
                )
                existing_links.add(key)
            if _conflicts_with_local(place, item):
                place.verification_status = "conflicting"
                if _CONFLICT_FLAG not in place.data_quality_flags:
                    # 不能用 ``place.data_quality_flags.append(...)``：ARRAY 列的原地改
                    # 不会被 SQLAlchemy 追踪，赋值新列表才会进 UPDATE。
                    place.data_quality_flags = [*place.data_quality_flags, _CONFLICT_FLAG]
                if place.canonical_name not in conflicts:
                    conflicts.append(place.canonical_name)
                continue
            step = Decimal(str(self.limits.search.confidence_step))
            current = Decimal(str(place.confidence)) if place.confidence is not None else Decimal("0.5")
            place.confidence = float(min(Decimal("1.0"), current + step))
            place.last_verified_at = now
            if place.canonical_name not in confirmed:
                confirmed.append(place.canonical_name)

        evidence.confirmed = tuple(confirmed)
        evidence.conflicts = tuple(conflicts)
        evidence.leads = tuple(leads)
        evidence.sources_written = created
        evidence.notes.extend(_notes(evidence, observations_by_url=observations_by_url))
        if leads:
            evidence.notes.append(
                "search:leads("
                + "、".join(leads[:5])
                + "：库外地点线索已登记，但搜索片段没有可信坐标，"
                "按 PRD §13.3 需补齐坐标后才能入候选池)"
            )
        log.info(
            "联网搜索完成",
            extra={
                "event": "plan.search_done",
                "context": {
                    "triggers": [trigger.kind for trigger in evidence.triggers],
                    "queries": len(queries),
                    "calls": evidence.calls,
                    "results": len(results),
                    "confirmed": len(confirmed),
                    "conflicts": len(conflicts),
                    "leads": len(leads),
                },
            },
        )

    async def _ensure_sources(
        self,
        results: Sequence[SearchResult],
        queries: Sequence[str],
        evidence: SearchEvidence,
    ) -> tuple[dict[str, TravelSource], int]:
        """按 ``url_hash`` 去重后登记 ``travel_sources``，返回 ``url_hash → 行``。"""
        wanted: dict[str, SearchResult] = {}
        for item in results:
            wanted.setdefault(sha256_hex(item.url), item)
        if not wanted:
            return {}, 0
        rows = (
            await self.db.execute(
                select(TravelSource).where(TravelSource.url_hash.in_(list(wanted)))
            )
        ).scalars().all()
        sources: dict[str, TravelSource] = {}
        for row in rows:
            if row.url_hash:
                sources.setdefault(row.url_hash, row)
        created = 0
        question = queries[0] if queries else ""
        for url_hash, item in wanted.items():
            if url_hash in sources:
                continue
            row = TravelSource(
                source_type="search_api",
                source_name=item.domain or self.provider_name,
                url=item.url,
                url_hash=url_hash,
                domain=item.domain,
                title=item.title or None,
                # 只留摘要片段的哈希与片段本身：不存网页全文（PRD §13.2 禁止项）
                content_hash=sha256_hex(item.snippet) if item.snippet else None,
                extracted_facts={
                    "schema": 1,
                    "query": question,
                    "snippet": item.snippet,
                    "observations": [],
                    "discovered_places": [],
                },
                credibility_score=self.scoring.search_source_credibility.score_for(item.domain),
                fetched_at=datetime.now(UTC),
            )
            self.db.add(row)
            sources[url_hash] = row
            created += 1
        if created:
            await self.db.flush()
        evidence.sources_written = created
        return sources, created

    async def _existing_links(self, place_ids: Sequence[uuid.UUID]) -> set[tuple[uuid.UUID, uuid.UUID]]:
        """已有的 ``place_sources`` 关系（避免撞唯一约束，也避免重复登记）。"""
        ids = [uuid.UUID(str(place_id)) for place_id in place_ids]
        if not ids:
            return set()
        rows = (
            await self.db.execute(
                select(PlaceSource.place_id, PlaceSource.source_id).where(
                    PlaceSource.place_id.in_(ids)
                )
            )
        ).all()
        return {(row[0], row[1]) for row in rows}

    # ── 库内事实 ──

    async def _load_rows(self, place_ids: Sequence[str]) -> list[Place]:
        ids = [uuid.UUID(str(place_id)) for place_id in place_ids]
        if not ids:
            return []
        rows = (await self.db.execute(select(Place).where(Place.id.in_(ids)))).scalars().all()
        return list(rows)

    def _name_index(self, rows: Sequence[Place]) -> dict[str, Place]:
        """``归一化名 → Place`` 索引（**只含本次候选**）。

        刻意只用候选：一个 5000 条地点的城市里，把每条搜索片段拿去和全城比对，
        既慢又没有意义 —— 用户拿到的方案只可能由候选组成。
        """
        return {normalize_name(row.canonical_name): row for row in rows}

    async def _alias_index(self, rows: Sequence[Place]) -> dict[str, Place]:
        """``别名归一化值 → Place``（PRD §14.2 Level 2 的别名匹配）。

        别名是建库时从 OSM/wikidata 的名称变体生成的，也正是搜索片段里最可能
        出现的写法（如"小蛮腰"之于"广州塔"）。
        """
        ids = [row.id for row in rows]
        if not ids:
            return {}
        links = (
            await self.db.execute(select(PlaceAlias).where(PlaceAlias.place_id.in_(ids)))
        ).scalars().all()
        by_id = {row.id: row for row in rows}
        index: dict[str, Place] = {}
        for link in links:
            place = by_id.get(link.place_id)
            if place is not None and link.alias_norm:
                index.setdefault(link.alias_norm, place)  # 同一别名指向多个地点时取第一个
        return index

    def _candidate_facts(self, row: Place, *, date_is_close: bool) -> _Candidate:
        hours_ttl = self.ttl.ttl_for("place_opening_hours")
        reference = row.last_verified_at or row.source_updated_at
        hours_stale = False
        if date_is_close and row.opening_hours is not None and reference is not None:
            age_hours = (datetime.now(UTC) - _as_aware(reference)).total_seconds() / 3600
            hours_stale = hours_ttl is not None and age_hours > hours_ttl
        return _Candidate(
            name=row.canonical_name,
            has_hours=row.opening_hours is not None,
            hours_stale=hours_stale,
            conflicting=row.verification_status == "conflicting"
            or _CONFLICT_FLAG in row.data_quality_flags,
        )

    def _travel_date_is_close(self, intent: Intent, *, today: date | None) -> bool:
        """出行是否临近（PRD §13.4 第 2 条：≤ ``travel_date_proximity_days`` 天才复验）。"""
        raw = intent.travel_date
        if not raw:
            return False
        try:
            travel = date.fromisoformat(raw)
        except ValueError:
            return False
        reference = today or datetime.now(UTC).date()
        window = _as_int(self.ttl.refresh_policy.get("travel_date_proximity_days"), 7)
        delta = (travel - reference).days
        return 0 <= delta <= window

    def _unknown_names(
        self, unparsed: Sequence[str], candidates: Sequence[DomainPlace]
    ) -> tuple[str, ...]:
        """规则引擎没解析出来的片段里，**看起来像地点名**的那些（PRD §13.2 实体查询）。

        ★ 为什么要做"看起来像"的过滤 ★
        ``unparsed`` 是"我们没看懂"的原文，里面什么都可能有（"随便走走"、
        "别太累"）。把它整段拿去搜索既浪费钱，又会让搜索词里出现一整个句子。
        判据只有两条：短（≤12 字）且库里没有同名的候选地点。
        """
        known = {normalize_name(candidate.name) for candidate in candidates}
        names: list[str] = []
        for fragment in unparsed:
            text = " ".join(str(fragment).split())
            if not text or len(text) > 12 or text in names:
                continue
            if normalize_name(text) in known:
                continue
            names.append(text)
        return tuple(names[:2])

    def _validated_observations(
        self,
        observations: Sequence[LlmSearchObservation],
        results: Sequence[SearchResult],
    ) -> tuple[LlmSearchObservation, ...]:
        """丢掉 URL 对不上、字段越界、值可疑的抽取结果（模型的话要过一遍校验）。"""
        urls = {item.url for item in results}
        kept: list[LlmSearchObservation] = []
        for item in observations:
            if item.url not in urls:
                continue
            if item.field not in SEARCH_FACT_FIELDS:
                continue
            value = " ".join(item.value.split())
            if not value:
                continue
            kept.append(
                LlmSearchObservation(
                    place_name=" ".join(item.place_name.split()),
                    field=item.field,
                    value=value[:200],
                    url=item.url,
                    confidence=item.confidence,
                )
            )
        return tuple(kept)


# ════════════════════════════════════════════════════════════════════════════
# 三、纯函数工具
# ════════════════════════════════════════════════════════════════════════════


def _as_results(value: Any) -> tuple[SearchResult, ...]:
    """把链的返回值（活的 ``SearchResult`` 或缓存里的 dict）统一成结果元组。"""
    raw: Any = value.get("results") if isinstance(value, Mapping) else value
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
        return ()
    items: list[SearchResult] = []
    for item in raw:
        if isinstance(item, SearchResult):
            items.append(item)
            continue
        if isinstance(item, Mapping):
            url = item.get("url")
            if not isinstance(url, str) or not url:
                continue
            items.append(
                SearchResult(
                    title=str(item.get("title", "")),
                    url=url,
                    snippet=str(item.get("snippet", "")),
                    domain=str(item["domain"]) if item.get("domain") else None,
                    score=float(item["score"]) if isinstance(item.get("score"), (int, float)) else None,
                    published_at=str(item["published_at"]) if item.get("published_at") else None,
                )
            )
    return tuple(items)


def _result_to_dict(item: SearchResult) -> dict[str, Any]:
    return {
        "title": item.title,
        "url": item.url,
        "snippet": item.snippet,
        "domain": item.domain,
        "score": item.score,
        "published_at": item.published_at,
    }


def _match_place(
    raw_name: str,
    index: Mapping[str, Place],
    *,
    aliases: Mapping[str, Place],
    floor: float,
) -> Place | None:
    """实体匹配（PRD §14）。搜索结果**只有文本没有坐标**，所以地理围栏用不上：

    精确归一化名 → 别名 → 模糊相似度（阈值来自 ``limits.search.entity_match_floor``）。
    匹配不上一律当"库外线索"，绝不强行归到某个近似地点上。
    """
    key = normalize_name(raw_name)
    if not key:
        return None
    exact = index.get(key) or aliases.get(key)
    if exact is not None:
        return exact
    best: Place | None = None
    best_score = 0.0
    for candidate_key, place in (*index.items(), *aliases.items()):
        if not candidate_key:
            continue
        score = similarity(key, candidate_key)
        if score >= floor and score > best_score:
            best, best_score = place, score
    return best


def _conflicts_with_local(place: Place, item: LlmSearchObservation) -> bool:
    """这条抽取结果是否**推翻**了库内已有事实（PRD §13.3 的"冲突"分支）。

    只比较库里**确实有值**的字段：库里是 NULL 时搜索结果只能算"补充线索"，
    谈不上冲突（``unknown`` ≠ ``conflicting``）。
    """
    local: str | None
    if item.field == "opening_hours":
        local = place.opening_hours_raw
    elif item.field == "price":
        local = place.price_note
    elif item.field == "status":
        local = "暂停营业" if place.status == "closed" else None
    else:
        return False
    if not local:
        return False
    left = _normalized_fact(local)
    right = _normalized_fact(item.value)
    if not left or not right:
        return False
    return left != right and left not in right and right not in left


def _normalized_fact(text: str) -> str:
    return text.strip().translate(_STRIP_CHARS).lower()


def _remember_discovered(
    source_row: TravelSource, item: LlmSearchObservation, *, results_by_url: Mapping[str, SearchResult]
) -> None:
    """把库外地点线索写进这条来源的 ``extracted_facts``（不建 places 行，见模块 docstring）。"""
    facts = dict(source_row.extracted_facts or {})
    discovered = list(facts.get("discovered_places") or [])
    if item.place_name not in discovered:
        discovered.append(item.place_name)
    facts["discovered_places"] = discovered
    observations = list(facts.get("observations") or [])
    observations.append(
        {
            "place_name": item.place_name,
            "field": item.field,
            "value": item.value,
            "url": item.url,
            "title": results_by_url[item.url].title if item.url in results_by_url else None,
        }
    )
    facts["observations"] = observations
    source_row.extracted_facts = facts


def _notes(
    evidence: SearchEvidence, *, observations_by_url: Mapping[str, Sequence[LlmSearchObservation]]
) -> list[str]:
    """本次搜索的降级说明（进 ``degraded_modes``，用户在界面上能看到）。"""
    notes: list[str] = []
    if evidence.triggers:
        labels = "、".join(trigger.label for trigger in evidence.triggers)
        notes.append(f"search:triggered({labels})")
    if evidence.queries:
        notes.append(
            f"search:queries({len(evidence.queries)} 次查询 · 调用 {evidence.calls} 次"
            + (f" · 缓存命中 {evidence.cache_hits} 次" if evidence.cache_hits else "")
            + f" · 结果 {evidence.result_count} 条)"
        )
    if evidence.confirmed:
        notes.append("search:confirmed(" + "、".join(evidence.confirmed[:5]) + ")")
    if evidence.conflicts:
        notes.append(
            "search:conflict("
            + "、".join(evidence.conflicts[:5])
            + "：搜索结果与库内说法不一致，已标 conflicting 并保留双方来源)"
        )
    if observations_by_url:
        notes.append(f"search:facts({sum(len(v) for v in observations_by_url.values())} 条事实已登记)")
    return notes


def _as_aware(value: datetime) -> datetime:
    """把 naive 时间当成 UTC：DB 里的时间列统一按 UTC 存，naive 只是读出来的形式。"""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
