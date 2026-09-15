"""规划编排（PRD §7.2 / §15.1）：把 M2 的纯函数内核、M3 的检索设施与数据库接起来。

这一层是**唯一允许同时认识数据库、Provider 与领域算法的地方**：

    意图解析（规则引擎）
      → L1 城市知识库 / L2 热门 / L3 模板 / L4 关系（检索链）
      → 候选生成 → 束搜索组合 → 可行性校验 → 评分
      → 落库（trip_requests / trips / trip_routes / trip_route_stops）

纪律：
1. **零外部调用优先**：本地知识库能出方案就绝不下沉到 L5–L8（PRD §15.1 铁律）。
   M4 的规划路径只用 L1–L4，因此一次规划的**外部成本恒为 0**（没有 Key 也完全可用）。
2. **数值一律由代码算**：路线名、推荐理由、预算都从数据推导，LLM 不参与数字。
3. **诚实性**：``degraded_modes``、``transport_source``、``budget.unknown_items``、
   ``feasibility_report`` 全部随结果返回 —— 不可信的数字必须自带"这是估算"的标注。
4. **幂等**：同 session 同参数在 ``limits.rate_limit.idempotency_window_s`` 内
   返回同一个 trip，不重复生成（PRD AC-13.4）。
5. **跨会话复用**：``plan_cache`` 命中的是别人的 trip 时，**复制一份**给当前 session，
   而不是把 trip_id 直接交出去（trip_id 是他人行程的访问凭据）。

刻意没做的事（写在代码里，避免"以为做了"）：
- 天气只作为评分乘数传入，M4 尚未拉取（``weather=None``）→ 天气乘数恒为 1.0。
  接入点已经留好（``plan_routes(weather=...)``），但"没拉天气"不等于"天气是晴"，
  所以这里不编造一个条件，只是不参与。
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from datetime import time as dt_time
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import (
    LimitsConfig,
    PricingConfig,
    ScoringConfig,
    Settings,
    TtlConfig,
    get_limits_config,
    get_pricing_config,
    get_scoring_config,
    get_settings,
    get_ttl_config,
)
from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger, scrub
from app.db.models import City, Place, Trip, TripRequest, TripRoute, TripRouteStop
from app.domain.budget import estimate_route_budget
from app.domain.candidates import Candidate, select_candidates, stay_duration_min
from app.domain.feasibility import FeasibilityReport, validate_route, weekday_of
from app.domain.intent import parse_intent
from app.domain.models import (
    ArchetypeName,
    BudgetSpec,
    Constraint,
    Intent,
    ParseResult,
    RelationIndex,
    RoutePlan,
    Stop,
    hhmm_to_minutes,
)
from app.domain.models import (
    Place as DomainPlace,
)
from app.domain.planner import effective_walking_cap, plan_routes
from app.domain.scoring import ScoreBreakdown, dimension_score, score_route
from app.domain.transit import leg_between
from app.providers.registry import Providers
from app.schemas.trips import PlanRequest
from app.services.cache import CacheStore, params_hash
from app.services.cost import CostLedger, CostStore, PriceBook
from app.services.kb_layers import (
    KIND_PLACES,
    KIND_RELATIONS,
    KIND_ROUTE_TEMPLATES,
    TemplateRoute,
    city_places_layer,
    latest_kb_version,
    load_city,
    relations_layer,
    route_templates_layer,
)
from app.services.llm_planner import (
    LlmPlanner,
    LlmStatus,
    LlmTrace,
    RouteNarrative,
    RouteNarrativeInput,
)
from app.services.rate_limit import RateLimiter
from app.services.retrieval_chain import RetrievalChain, RetrievalQuery
from app.services.retrieval_layers import CacheLayer, LlmLayer

__all__ = [
    "ARCHETYPES",
    "PlanOutcome",
    "PlanProgress",
    "PlanService",
    "intent_to_dict",
]

log = get_logger("plan")

ARCHETYPES: tuple[ArchetypeName, ...] = ("relaxed", "classic", "themed")
ARCHETYPE_LABELS: dict[str, str] = {"relaxed": "轻松", "classic": "经典", "themed": "主题"}
ROUTE_LABELS: tuple[str, ...] = ("A", "B", "C", "D")

#: 评分维度的中文名（"为什么推荐"面板与 pros 用它，不暴露内部字段名）
DIMENSION_LABELS: dict[str, str] = {
    "preference": "偏好匹配",
    "efficiency": "路线效率",
    "time_fit": "时间安排",
    "popularity": "热门程度",
    "budget_fit": "预算匹配",
    "walking_fit": "步行强度",
    "place_relation": "地点组合",
}


@dataclass(frozen=True, slots=True)
class PlanProgress:
    """SSE ``plan.progress`` 事件的载荷（PRD §7.3）。"""

    stage: int
    key: str
    label: str
    pct: int
    detail: Mapping[str, Any] = field(default_factory=dict)


ProgressSink = Callable[[PlanProgress], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class PlanOutcome:
    request_id: uuid.UUID
    trip_id: uuid.UUID
    route_count: int
    degraded_modes: tuple[str, ...]
    cached: bool
    elapsed_ms: int
    #: 本次请求里 LLM 的**真实**使用情况（配了 Key 却降级时，原因也在这里）。
    #: 默认 None 只为兼容旧调用方，``PlanService.plan`` 永远会填上它。
    llm: LlmStatus | None = None


@dataclass(frozen=True, slots=True)
class _ScoredPlan:
    """候选方案 + 它的评分与校验报告。"""

    plan: RoutePlan
    archetype: ArchetypeName
    report: FeasibilityReport
    breakdown: ScoreBreakdown
    source: str  # generated | template
    template_route_id: uuid.UUID | None = None
    theme: str | None = None


# ════════════════════════════════════════════════════════════════════════════
# 意图构造
# ════════════════════════════════════════════════════════════════════════════


def intent_to_dict(intent: Intent) -> dict[str, Any]:
    """意图 → 可写入 jsonb 的普通 dict（含人能看懂的时间与预算口径）。"""
    return {
        "city": intent.city,
        "days": intent.days,
        "people": intent.people,
        "preferences": {k: float(v) for k, v in intent.preferences.items()},
        "pace": intent.pace,
        "budget": {
            "amount": None if intent.budget.amount is None else str(intent.budget.amount),
            "scope": intent.budget.scope,
            "currency": intent.budget.currency,
        },
        "start_min": intent.start_min,
        "end_min": intent.end_min,
        "start_time": _hhmm(intent.start_min),
        "end_time": _hhmm(intent.end_min),
        "travel_date": intent.travel_date,
        "weather_sensitive": intent.weather_sensitive,
    }


def _hhmm(minutes: int) -> str:
    return f"{(minutes // 60) % 24:02d}:{minutes % 60:02d}"


def _form_summary(payload: PlanRequest) -> dict[str, Any]:
    """给模型的“表单已知信息”（自由文本单独提供，不混进来）。"""
    budget: dict[str, Any] | None = None
    if payload.budget is not None and payload.budget.amount is not None:
        budget = {"amount": str(payload.budget.amount), "scope": payload.budget.scope}
    return {
        "city": payload.city,
        "days": payload.days,
        "people": payload.people,
        "preferences": list(payload.preferences),
        "pace": payload.pace,
        "budget": budget,
        "start_time": payload.start_time,
        "end_time": payload.end_time,
        "travel_date": payload.travel_date,
    }


def _llm_calibrated(ledger: CostLedger) -> bool:
    """LLM 单价是否已校准。没有任何 LLM 记录时算“已校准”—— 没花钱就谈不上不准。"""
    entries = [entry for entry in ledger.entries if entry.category == "llm"]
    return all(entry.calibrated for entry in entries)


def _parse_time(value: str | None, *, field_name: str) -> int | None:
    if value is None:
        return None
    try:
        return hhmm_to_minutes(value)
    except ValueError as exc:
        raise AppError(
            ErrorCode.INVALID_INPUT,
            f"{field_name} 不是合法时间：{value!r}",
            hint="时间请用 HH:MM 格式，例如 09:00。",
        ) from exc


def build_intent(
    payload: PlanRequest, *, limits: LimitsConfig, scoring: ScoringConfig
) -> tuple[Intent, tuple[Constraint, ...], ParseResult]:
    """把表单输入 + 自由文本解析成结构化意图与约束。

    自由文本里的规则**可以覆盖**表单值（PRD FR-02：用户后说的为准），
    因此这里先把表单转成 ``base_intent``，再让规则引擎在其上做增量更新。
    """
    free_text = payload.free_text.strip()
    max_chars = limits.rate_limit.input_free_text_max_chars
    if len(free_text) > max_chars:
        raise AppError(
            ErrorCode.INVALID_INPUT,
            f"补充说明过长（{len(free_text)} 字），最多 {max_chars} 字",
            hint="把最重要的偏好写前面即可，其余会在规划时按默认值处理。",
            context={"max_chars": max_chars},
        )

    unknown_prefs = [p for p in payload.preferences if p not in scoring.preference_dimensions]
    if unknown_prefs:
        raise AppError(
            ErrorCode.INVALID_INPUT,
            f"未知偏好：{', '.join(sorted(unknown_prefs))}",
            hint=f"合法偏好：{', '.join(sorted(scoring.preference_dimensions))}",
        )

    default_start = limits.planning.default_window["start"]
    default_end = limits.planning.default_window["end"]
    start_min = _parse_time(payload.start_time, field_name="start_time") or hhmm_to_minutes(default_start)
    end_min = _parse_time(payload.end_time, field_name="end_time") or hhmm_to_minutes(default_end)
    if start_min >= end_min:
        raise AppError(
            ErrorCode.INVALID_INPUT,
            "开始时间必须早于结束时间",
            hint="一天的行程至少要有 1 小时可用时间。",
            context={"start_min": start_min, "end_min": end_min},
        )

    budget = BudgetSpec(
        amount=payload.budget.amount if payload.budget else None,
        scope=payload.budget.scope if payload.budget else "per_person",
    )
    base = Intent(
        city=payload.city,
        days=payload.days,
        people=payload.people,
        preferences=dict.fromkeys(payload.preferences, 1.0),
        pace=payload.pace,
        budget=budget,
        start_min=start_min,
        end_min=end_min,
        travel_date=payload.travel_date,
    )
    parsed = parse_intent(free_text, base, limits=limits, scoring=scoring)
    return parsed.intent, parsed.constraints, parsed


# ════════════════════════════════════════════════════════════════════════════
# 编排
# ════════════════════════════════════════════════════════════════════════════


@dataclass
class PlanService:
    db: AsyncSession
    session_id: uuid.UUID
    providers: Providers
    scoring: ScoringConfig = field(default_factory=get_scoring_config)
    limits: LimitsConfig = field(default_factory=get_limits_config)
    ttl: TtlConfig = field(default_factory=get_ttl_config)
    settings: Settings = field(default_factory=get_settings)
    pricing: PricingConfig = field(default_factory=get_pricing_config)

    # ── 对外入口 ──

    async def plan(
        self,
        payload: PlanRequest,
        *,
        on_progress: ProgressSink | None = None,
        extra_constraints: Sequence[Constraint] = (),
        ip_hash: str | None = None,
        user_agent_hash: str | None = None,
        request_id: uuid.UUID | None = None,
        rate_limit_keys: Sequence[tuple[str, int, int]] = (),
        parent_trip: Trip | None = None,
        allow_cache: bool = True,
    ) -> PlanOutcome:
        started = time.perf_counter()
        await self._emit(on_progress, PlanProgress(0, "started", "开始规划", 0, {"stages": 4}))

        city = await load_city(self.db, payload.city)
        kb_version = await latest_kb_version(self.db, city.id)
        intent, constraints, parsed = build_intent(payload, limits=self.limits, scoring=self.scoring)

        # ★ LLM 参与规划的两个位置都在这里装配（见 llm_planner 的 docstring）★
        # 意图补全必须发生在算指纹**之前**：幂等键与缓存键都要由“最终意图”生成，
        # 否则规则解析与 LLM 解析会共用同一个键，把两种不同的需求混成一条缓存。
        # 重复提交不会重复花钱 —— L5 的 LLM 缓存会直接命中（prompt 版本进缓存键）。
        ledger = CostLedger(
            price_book=PriceBook(self.pricing),
            breaker=self.limits.cost.circuit_breaker,
            providers=self.limits.providers,
        )
        # ★ PRD §15.4 第四级熔断：全局日成本超限 ⇒ 进入「缓存优先模式」（不调模型/不联网）★
        # 判断必须在**建链之前**：链一旦跑起来钱就已经花了。
        # 「单次规划」的熔断器只看本次，看不住一天里很多次堆起来的账单 —— 这一级才是兜底。
        budget = await CostStore(self.db).daily_budget(
            limit_cny=self.limits.cost.global_daily_cny
        )
        trace = LlmTrace(
            # 预算用完就明确不调，不是"降级然后碰碰运气"
            enabled=self.providers.llm.health().available and not budget.exceeded,
            provider=self.providers.llm.name,
        )
        chain = self._build_chain(city.id, ledger=ledger)
        planner = LlmPlanner(
            chain=chain,
            store=CacheStore(self.db),
            ledger=ledger,
            provider=self.providers.llm,
            trace=trace,
            ttl=self.ttl,
            max_output_tokens=self.limits.providers.llm_max_output_tokens,
            schema_retries=self.limits.providers.llm_max_retries,
        )
        if payload.free_text.strip():
            parsed = await planner.refine_intent(
                parsed,
                free_text=payload.free_text,
                form_summary=_form_summary(payload),
                allowed_preferences=tuple(self.scoring.preference_dimensions),
            )

        intent = parsed.intent
        constraints = parsed.constraints
        if extra_constraints:
            constraints = (*constraints, *extra_constraints)

        await self._emit(
            on_progress,
            PlanProgress(
                1,
                "intent",
                "理解你的偏好",
                15,
                {
                    "parse_source": parsed.parse_source,
                    "unparsed": len(parsed.unparsed),
                    # 这一步到底用了规则还是模型，进度事件里就如实写出来
                    "llm": trace.tasks.get("intent_patch", "skipped"),
                },
            ),
        )

        fingerprint = params_hash(
            city=payload.city,
            days=intent.days,
            people=intent.people,
            preferences=tuple(intent.active_preferences),
            pace=intent.pace,
            budget_scope=intent.budget.scope,
            budget_amount=intent.budget.amount,
            start_time=_hhmm(intent.start_min),
            end_time=_hhmm(intent.end_min),
            travel_date=intent.travel_date,
            # 必须带上**取值**而不只是类型："排除广州塔"与"排除白云山"
            # 若生成同一个键，幂等与缓存就会把一次请求当成另一次。
            constraints=[f"{c.type}:{c.value}" for c in constraints],
        )

        if allow_cache:
            replay = await self._replay(fingerprint, trace=trace, ledger=ledger)
            if replay is not None:
                return replay
            cached = await self._reuse_cache(
                fingerprint, kb_version, payload, trace=trace, ledger=ledger
            )
            if cached is not None:
                return cached

        # 限流放在"确认这是冷规划"之后：缓存命中与幂等重放**不该消耗配额**，
        # 否则用户刷新一下页面就被自己的重复提交挡住了。
        # 注意计数器与规划共用同一个事务：规划失败回滚时计数也回滚
        # （"失败不扣配额"是刻意选择）。
        await self._enforce_rate_limits(rate_limit_keys)

        degraded: list[str] = []
        if budget.exceeded:
            # 说出来：钱花完了才降级，与"本来就没配 Key"是两件事
            degraded.append(budget.degraded_reason())

        places = await self._resolve_places(chain, city, degraded)
        if not places:
            raise AppError(
                ErrorCode.NO_PLACES_FOR_CITY,
                f"{city.name} 的知识库里还没有可用地点",
                hint="请先执行 make seed 建库，或换一个城市。",
                context={"city": payload.city},
            )

        candidate_set = select_candidates(
            places, intent, constraints, scoring=self.scoring, limits=self.limits
        )
        candidates = candidate_set.items
        await self._emit(
            on_progress,
            PlanProgress(
                2,
                "candidates",
                f"筛选 {city.name} {len(places)} 个地点",
                40,
                {"candidates": len(candidates), "relaxed": list(candidate_set.relaxed_reasons)},
            ),
        )
        if len(candidates) < self.limits.planning.candidate_min:
            raise AppError(
                ErrorCode.CANDIDATES_INSUFFICIENT,
                f"符合条件的地点只有 {len(candidates)} 个，排不出合理路线",
                hint="试着放宽预算、减少排除项，或降低步行限制。",
                context={
                    "candidates": len(candidates),
                    "candidate_min": self.limits.planning.candidate_min,
                    "relaxed_reasons": list(candidate_set.relaxed_reasons),
                },
            )

        place_ids = [c.place.id for c in candidates]
        template_outcome = await chain.resolve(
            RetrievalQuery(kind=KIND_ROUTE_TEMPLATES, key=city.slug, payload={})
        )
        templates: list[TemplateRoute] = list(template_outcome.value or [])
        relation_outcome = await chain.resolve(
            RetrievalQuery(kind=KIND_RELATIONS, key=city.slug, payload={"place_ids": place_ids})
        )
        relations = RelationIndex.build(relation_outcome.value or [])
        if template_outcome.degraded or relation_outcome.degraded:
            degraded.append("retrieval:partial(部分检索层失败，已降级)")

        walking_cap = effective_walking_cap(intent, constraints, self.limits)
        weekday = weekday_of(intent.travel_date)

        scored = self._compose(candidates, templates, intent, constraints, relations, walking_cap, weekday)
        await self._emit(
            on_progress,
            PlanProgress(
                3,
                "validate",
                "校验路线可行性",
                70,
                # 只报"通过校验的方案数"：``_score_plan`` 已经把不可行的方案丢掉了，
                # 再报一个 checked 等于声称"所有尝试过的方案都通过了"。
                {"feasible": len(scored)},
            ),
        )
        selected = self._select(scored)
        if not selected:
            raise AppError(
                ErrorCode.NO_FEASIBLE_ROUTE,
                "在你给出的限制下排不出可行的路线",
                hint="试着放宽预算、步行量或减少偏好/排除项。",
                context={"candidates": len(candidates)},
            )

        await self._emit(
            on_progress,
            PlanProgress(4, "compose", f"生成 {len(selected)} 套方案", 90, {"routes": len(selected)}),
        )

        # 方案叙事：只改写文案，不动任何数字；失败/不合格的文案由模板兜底。
        narrative = await planner.narrate(
            [
                RouteNarrativeInput(
                    index=order,
                    archetype=item.archetype,
                    stop_names=tuple(stop.place.name for stop in item.plan.stops),
                    pace=intent.pace,
                    preferences=tuple(intent.active_preferences),
                )
                for order, item in enumerate(selected)
            ]
        )

        degraded.extend(self.providers.degraded_modes())
        # 配了 Key 却没用上（超时/限流/schema 不符/成本熔断）也是降级，必须说出来：
        # 静默地“降级但看着一切正常”比直接报错更坑人。
        if trace.enabled:
            degraded.extend(f"llm:{reason}" for reason in trace.fallback_reasons)
        note = self._route_count_note(len(selected))
        return await self._persist(
            city=city,
            payload=payload,
            parsed=parsed,
            intent=intent,
            constraints=constraints,
            fingerprint=fingerprint,
            kb_version=kb_version,
            selected=selected,
            narrative=narrative,
            ledger=ledger,
            trace=trace,
            degraded=degraded,
            note=note,
            started=started,
            ip_hash=ip_hash,
            user_agent_hash=user_agent_hash,
            request_id=request_id,
            parent_trip=parent_trip,
        )

    async def _enforce_rate_limits(self, keys: Sequence[tuple[str, int, int]]) -> None:
        """固定窗口限流（PRD §15.4）。``limit <= 0`` 表示该限制未启用。"""
        limiter = RateLimiter(self.db)
        for key, limit, window_s in keys:
            result = await limiter.hit(key, limit=limit, window_s=window_s)
            if not result.allowed:
                raise AppError(
                    ErrorCode.RATE_LIMITED,
                    "今天的规划次数已达上限",
                    hint=f"请在约 {max(1, result.retry_after_s // 60)} 分钟后再试。",
                    context={"used": result.used, "limit": result.limit},
                )

    # ── 幂等与缓存 ──

    async def _replay(
        self, fingerprint: str, *, trace: LlmTrace, ledger: CostLedger
    ) -> PlanOutcome | None:
        """同 session 同参数在幂等窗口内 → 返回同一个 trip（不重复计费）。"""
        window_s = self.limits.rate_limit.idempotency_window_s
        if window_s <= 0:
            return None
        since = datetime.now(UTC) - timedelta(seconds=window_s)
        row = (
            await self.db.execute(
                select(TripRequest)
                .where(
                    TripRequest.session_id == self.session_id,
                    TripRequest.params_hash == fingerprint,
                    TripRequest.status.in_(("completed", "degraded")),
                    TripRequest.created_at >= since,
                )
                .order_by(TripRequest.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if row is None or row.elapsed_ms is None:
            return None
        trip = (
            await self.db.execute(select(Trip).where(Trip.request_id == row.id).limit(1))
        ).scalar_one_or_none()
        if trip is None:
            return None
        # 复用结果也要把这一次真正花掉的钱落库（意图补全可能已经调过模型）。
        await self._persist_cost(ledger, request_id=row.id, trip_id=trip.id)
        return PlanOutcome(
            request_id=row.id,
            trip_id=trip.id,
            route_count=trip.route_count,
            degraded_modes=tuple(trip.degraded_modes or ()),
            cached=True,
            elapsed_ms=row.elapsed_ms,
            llm=self._llm_status(trace, ledger, cached=True),
        )

    async def _reuse_cache(
        self,
        fingerprint: str,
        kb_version: str,
        payload: PlanRequest,
        *,
        trace: LlmTrace,
        ledger: CostLedger,
    ) -> PlanOutcome | None:
        """跨会话复用 ``plan_cache``：命中别人的 trip 时复制一份，而不是交出 trip_id。"""
        cached_id = await CacheStore(self.db).get_plan(fingerprint, kb_version)
        if cached_id is None:
            return None
        source = (await self.db.execute(select(Trip).where(Trip.id == cached_id))).scalar_one_or_none()
        if source is None:
            return None
        if source.session_id == self.session_id:
            await self._persist_cost(ledger, request_id=source.request_id, trip_id=source.id)
            return PlanOutcome(
                request_id=source.request_id,
                trip_id=source.id,
                route_count=source.route_count,
                degraded_modes=tuple(source.degraded_modes or ()),
                cached=True,
                elapsed_ms=source.generation_ms or 0,
                llm=self._llm_status(trace, ledger, cached=True),
            )
        copied = await self._copy_trip(source, payload, fingerprint=fingerprint)
        await self._persist_cost(ledger, request_id=copied.request_id, trip_id=copied.id)
        return PlanOutcome(
            request_id=copied.request_id,
            trip_id=copied.id,
            route_count=copied.route_count,
            degraded_modes=tuple(copied.degraded_modes or ()),
            cached=True,
            elapsed_ms=copied.generation_ms or 0,
            llm=self._llm_status(trace, ledger, cached=True),
        )

    async def _persist_cost(
        self, ledger: CostLedger, *, request_id: uuid.UUID | None, trip_id: uuid.UUID | None
    ) -> None:
        """把账本里的外部调用写进 ``cost_logs``（成本报表靠它，不靠日志里的声明）。"""
        if not ledger.entries:
            return
        await CostStore(self.db).persist(ledger.entries, request_id=request_id, trip_id=trip_id)

    def _llm_status(
        self, trace: LlmTrace, ledger: CostLedger, *, cached: bool
    ) -> LlmStatus:
        """本次请求的 LLM 使用情况（缓存复用时用 ``tasks`` 说明“没为此重调模型”）。"""
        if cached:
            trace.note_task("result", "cache")
        return trace.status(
            cost_cny=ledger.spent_cny("llm"), cost_calibrated=_llm_calibrated(ledger)
        )

    async def _copy_trip(self, source: Trip, payload: PlanRequest, *, fingerprint: str) -> Trip:
        """把一次命中的行程复制给当前 session（路线/站点逐行复制，快照不变）。

        ★ ``params_hash`` 必须与调用方的 ``fingerprint`` **完全一致** ★
        早期实现在这里用意图快照重新算了一个键（还漏掉了约束），于是副本对
        ``_replay`` 不可见：同一个会话重复提交同一份需求时，每次都重新复制一份，
        既不幂等（AC-13.4），也在数据库里堆出重复行程。同时补上 ``elapsed_ms``：
        ``_replay`` 靠它区分"跑完的规划"与"中途失败留下的行"。
        """
        request = TripRequest(
            session_id=self.session_id,
            raw_input=payload.model_dump(mode="json"),
            intent=source.intent_snapshot,
            params_hash=fingerprint,
            status="completed",
            completed_at=datetime.now(UTC),
            elapsed_ms=source.generation_ms or 0,
        )
        self.db.add(request)
        await self.db.flush()

        trip = Trip(
            request_id=request.id,
            session_id=self.session_id,
            city_id=source.city_id,
            title=source.title,
            intent_snapshot=source.intent_snapshot,
            days=source.days,
            route_count=source.route_count,
            generation_ms=source.generation_ms,
            total_cost_cny=source.total_cost_cny,
            degraded_modes=list(source.degraded_modes or []),
            revision_no=source.revision_no,
            parent_trip_id=source.id,
            source="plan_cache",
        )
        self.db.add(trip)
        await self.db.flush()

        routes = (
            await self.db.execute(select(TripRoute).where(TripRoute.trip_id == source.id))
        ).scalars().all()
        for route in routes:
            new_route = TripRoute(
                trip_id=trip.id,
                label=route.label,
                archetype=route.archetype,
                theme=route.theme,
                name=route.name,
                one_liner=route.one_liner,
                total_duration_min=route.total_duration_min,
                total_distance_m=route.total_distance_m,
                walking_distance_m=route.walking_distance_m,
                transit_time_min=route.transit_time_min,
                transit_distance_m=route.transit_distance_m,
                budget_min=route.budget_min,
                budget_max=route.budget_max,
                budget_scope=route.budget_scope,
                place_count=route.place_count,
                recommend_score=route.recommend_score,
                score_breakdown=route.score_breakdown,
                best_for=list(route.best_for or []),
                highlights=list(route.highlights or []),
                pros=list(route.pros or []),
                cons=list(route.cons or []),
                recommendation_reason=route.recommendation_reason,
                feasibility_report=route.feasibility_report,
                polyline=route.polyline,
                route_source=route.route_source,
                template_route_id=route.template_route_id,
                sort_order=route.sort_order,
            )
            self.db.add(new_route)
            await self.db.flush()
            stops = (
                await self.db.execute(
                    select(TripRouteStop)
                    .where(TripRouteStop.trip_route_id == route.id)
                    .order_by(TripRouteStop.seq)
                )
            ).scalars().all()
            for stop in stops:
                self.db.add(
                    TripRouteStop(
                        trip_route_id=new_route.id,
                        seq=stop.seq,
                        place_id=stop.place_id,
                        place_snapshot=stop.place_snapshot,
                        arrive_time=stop.arrive_time,
                        depart_time=stop.depart_time,
                        stay_min=stop.stay_min,
                        transport_mode=stop.transport_mode,
                        transport_min=stop.transport_min,
                        transport_distance_m=stop.transport_distance_m,
                        transport_source=stop.transport_source,
                        why_recommended=stop.why_recommended,
                        tips=stop.tips,
                        source_refs=stop.source_refs,
                        warnings=list(stop.warnings or []),
                    )
                )
        await self.db.flush()
        return trip

    # ── 组合与选择 ──

    def _build_chain(self, city_id: uuid.UUID, *, ledger: CostLedger) -> RetrievalChain:
        """装配本次规划用到的检索层。

        ★ 为什么链上没有 L2（热门地点库）★
        在现在的数据模型里，"热门地点"**不是一份独立数据** ——
        它就是 ``places`` 表按 ``popularity_score`` 排序，与 L1 同一张表。
        把它再装一层、再查一次库，除了让层报表好看一点没有任何收益，
        反而给"同一份数据查了两遍"开了先例。热度确实参与了规划，
        但在 ``select_candidates`` 的 ``0.4 × popularity`` 里参与，
        而不是靠一个同义层。详见 ``services/kb_layers.py`` 的说明。
        """
        return RetrievalChain(
            layers=[
                # L5：LLM 结果缓存。**排在模型之前** —— 命中就完全不花钱，
                # 重复提交、多会话同需求都靠它把成本压到 0。
                CacheLayer(store=CacheStore(self.db)),
                city_places_layer(self.db, city_id, self.scoring),
                route_templates_layer(self.db, city_id),
                relations_layer(self.db, city_id),
                # L7：AI 推理。只被 llm_planner 以 KIND_LLM 驱动（意图/叙事）。
                # 未配 Key 时它是 NullLlmProvider，会抛 PROVIDER_UNAVAILABLE →
                # 由链记一次失败并让上层降级到规则引擎（PRD §15.5）。
                LlmLayer(provider=self.providers.llm, ledger=ledger),
            ]
        )

    async def _resolve_places(
        self, chain: RetrievalChain, city: City, degraded: list[str]
    ) -> list[DomainPlace]:
        outcome = await chain.resolve(RetrievalQuery(kind=KIND_PLACES, key=city.slug))
        if outcome.degraded:
            degraded.append("retrieval:L1(知识库查询降级)")
        return list(outcome.value or [])

    def _compose(
        self,
        candidates: Sequence[Candidate],
        templates: Sequence[TemplateRoute],
        intent: Intent,
        constraints: Sequence[Constraint],
        relations: RelationIndex,
        walking_cap: int,
        weekday: int | None,
    ) -> list[_ScoredPlan]:
        """束搜索 + 模板复用，产出**已通过可行性校验**的方案（含评分）。"""
        by_id = {c.place.id: c for c in candidates}
        scored: list[_ScoredPlan] = []

        for template in templates:
            plan = self._template_plan(template, by_id, intent, relations)
            if plan is None:
                continue
            entry = self._score_plan(
                plan,
                template.archetype if template.archetype in ARCHETYPES else "classic",
                intent,
                constraints,
                relations,
                walking_cap,
                weekday,
                source="template",
                template_route_id=uuid.UUID(template.route_id),
                theme=template.name,
            )
            if entry is not None:
                scored.append(entry)

        for archetype in ARCHETYPES:
            for plan in plan_routes(
                candidates,
                intent,
                constraints,
                relations=relations,
                limits=self.limits,
                scoring=self.scoring,
                archetype=archetype,
            ):
                entry = self._score_plan(
                    plan,
                    archetype,
                    intent,
                    constraints,
                    relations,
                    walking_cap,
                    weekday,
                    source="generated",
                )
                if entry is not None:
                    scored.append(entry)
        return scored

    def _template_plan(
        self,
        template: TemplateRoute,
        by_id: Mapping[str, Candidate],
        intent: Intent,
        relations: RelationIndex,
    ) -> RoutePlan | None:
        """把模板的站点顺序套到本次候选上（模板里缺失的站点直接跳过）。

        跳过后若剩余站点不足 ``min_route_stops``，或模板站点被保留的比例低于 60%，
        就放弃这条模板 —— 一条被删掉一半的"经典路线"已经不是它自己了。
        """
        ordered = [by_id[pid].place for pid in template.place_ids if pid in by_id]
        if len(ordered) < self.limits.planning.min_route_stops:
            return None
        if len(ordered) / max(1, len(template.place_ids)) < 0.6:
            return None

        stays = dict(zip(template.place_ids, template.stay_min, strict=False))
        stops: list[Stop] = []
        arrive = intent.start_min
        for index, place in enumerate(ordered):
            stay = stays.get(place.id) or stay_duration_min(place)
            stop = Stop(place=place, arrive_min=arrive, stay_min=stay)
            stops.append(stop)
            if index < len(ordered) - 1:
                leg = leg_between(place, ordered[index + 1], relations, self.limits.travel_modes)
                stops[-1] = Stop(place=place, arrive_min=arrive, stay_min=stay, leg_to_next=leg)
                arrive = arrive + stay + leg.minutes
        return RoutePlan(archetype="classic", stops=tuple(stops), theme=template.name)

    def _score_plan(
        self,
        plan: RoutePlan,
        archetype: ArchetypeName,
        intent: Intent,
        constraints: Sequence[Constraint],
        relations: RelationIndex,
        walking_cap: int,
        weekday: int | None,
        *,
        source: str,
        template_route_id: uuid.UUID | None = None,
        theme: str | None = None,
    ) -> _ScoredPlan | None:
        """补算预算 → 校验 → 评分。不可行的方案在这里被丢弃（不返回）。"""
        budget = plan.budget or estimate_route_budget(plan.stops, intent, self.limits.budget)
        report = validate_route(
            plan.stops,
            intent,
            limits=self.limits,
            scoring=self.scoring,
            budget=budget,
            walking_cap_m=walking_cap,
            weekday=weekday,
        )
        if not report.feasible:
            return None
        breakdown = score_route(
            plan.stops,
            intent,
            scoring=self.scoring,
            archetype=archetype,
            relations=relations,
            budget=budget,
            walking_cap_m=walking_cap,
        )
        return _ScoredPlan(
            plan=RoutePlan(archetype=archetype, stops=plan.stops, theme=theme, budget=budget),
            archetype=archetype,
            report=report,
            breakdown=breakdown,
            source=source,
            template_route_id=template_route_id,
            theme=theme,
        )

    def _select(self, scored: Sequence[_ScoredPlan]) -> list[_ScoredPlan]:
        """选最终方案：**每个 archetype 先占一个名额**，再按总分补满，全程 Jaccard 去重。

        ★ 为什么不能只按总分排 ★
        PRD FR-06 承诺的是三套**定位不同**的方案（A 轻松休闲 / B 经典打卡 / C 主题型），
        AC-6.3 还要求 ``best_for`` 必须差异化。纯按总分取前三名时，
        经常三套全是 classic —— 标签、适合人群、推荐理由全部雷同，
        用户看到的三个 Tab 其实是同一条路线的三种摆法。
        （实测就是如此：第一版输出 "经典路线 …" × 3。）

        代价：某个 archetype 的最优方案可能比分更高的 classic 方案差，
        但它换来的是"三套方案真的不一样"。如果某个 archetype 排不出可行方案，
        就少一套并由 ``route_count_note`` 如实解释，而不是拿另一套 classic 充数。
        """
        max_routes = self.limits.planning.max_output_routes
        ordered = sorted(scored, key=lambda item: item.breakdown.total, reverse=True)
        kept: list[_ScoredPlan] = []
        taken: set[int] = set()

        def take(item: _ScoredPlan) -> bool:
            if id(item) in taken:
                return False
            ids = {stop.place.id for stop in item.plan.stops}
            if any(
                _jaccard(ids, {s.place.id for s in other.plan.stops})
                > self.limits.planning.max_route_similarity
                for other in kept
            ):
                return False
            kept.append(item)
            taken.add(id(item))
            return True

        for archetype in ARCHETYPES:
            if len(kept) >= max_routes:
                break
            for item in ordered:
                if item.archetype == archetype and take(item):
                    break

        for item in ordered:
            if len(kept) >= max_routes:
                break
            take(item)
        return kept

    def _route_count_note(self, count: int) -> str | None:
        if count >= self.limits.planning.max_output_routes:
            return None
        return (
            f"符合条件的方案只有 {count} 套（期望 {self.limits.planning.max_output_routes} 套）。"
            "放宽预算、步行限制或减少排除项可以拿到更多选择。"
        )

    # ── 落库 ──

    async def _persist(
        self,
        *,
        city: City,
        payload: PlanRequest,
        parsed: ParseResult,
        intent: Intent,
        constraints: Sequence[Constraint],
        fingerprint: str,
        kb_version: str,
        selected: Sequence[_ScoredPlan],
        narrative: Mapping[int, RouteNarrative],
        ledger: CostLedger,
        trace: LlmTrace,
        degraded: Sequence[str],
        note: str | None,
        started: float,
        ip_hash: str | None,
        user_agent_hash: str | None,
        request_id: uuid.UUID | None,
        parent_trip: Trip | None,
    ) -> PlanOutcome:
        free_text = payload.free_text.strip()
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        request = TripRequest(
            # 由 API 层预先生成：202 响应里的 request_id 必须与落库的那条一致，
            # 否则 SSE 订阅（按 request_id）会永远等不到事件。
            id=request_id or uuid.uuid4(),
            session_id=self.session_id,
            raw_input=payload.model_dump(mode="json"),
            free_text_len=len(free_text) or None,
            # 不存全文：只留脱敏后的前 200 字（PRD AC-12.5）
            free_text_masked=scrub(free_text, max_len=200) if free_text else None,
            intent=intent_to_dict(intent),
            constraints=[
                {"type": c.type, "value": c.value, "raw": c.raw, "source": c.source} for c in constraints
            ],
            parse_source=parsed.parse_source,
            params_hash=fingerprint,
            status="degraded" if degraded else "completed",
            completed_at=datetime.now(UTC),
            elapsed_ms=elapsed_ms,
            ip_hash=ip_hash,
            user_agent_hash=user_agent_hash,
        )
        self.db.add(request)
        await self.db.flush()

        stops_ids = [stop.place.id for item in selected for stop in item.plan.stops]
        snapshots = await self._stop_snapshots(stops_ids)

        trip = Trip(
            request_id=request.id,
            session_id=self.session_id,
            city_id=city.id,
            title=f"{city.name} · {_hhmm(intent.start_min)}–{_hhmm(intent.end_min)}",
            intent_snapshot={
                **intent_to_dict(intent),
                "route_count_note": note,
                "selected": len(selected),
            },
            days=intent.days,
            route_count=len(selected),
            generation_ms=elapsed_ms,
            # 只统计**真实发生**的外部调用成本（本地知识库查询不计）。
            # 配了 LLM Key 时这里不再是 0，而是一个带 calibrated 标记的量级值。
            total_cost_cny=ledger.spent_cny(),
            degraded_modes=list(degraded),
            # 修改产生的版本挂在上一版下面：撤销/回看都是沿这条链走
            revision_no=(parent_trip.revision_no + 1) if parent_trip else 1,
            parent_trip_id=parent_trip.id if parent_trip else None,
            source="revision" if parent_trip else "generated",
        )
        self.db.add(trip)
        await self.db.flush()

        for order, item in enumerate(selected):
            # 文案优先级：LLM 写的（已过数字/长度校验）> 模板（代码推导）。
            # 数字类字段（one_liner 里的站数/时长/步行）永远由代码算，模型不许碰。
            chosen = narrative.get(order)
            route = TripRoute(
                trip_id=trip.id,
                label=ROUTE_LABELS[order],
                archetype=item.archetype,
                theme=item.theme,
                name=chosen.name if chosen is not None else _route_name(item),
                one_liner=_one_liner(item),
                total_duration_min=item.plan.metrics.total_duration_min,
                total_distance_m=item.plan.metrics.transit_distance_m,
                walking_distance_m=item.plan.metrics.walking_m,
                transit_time_min=item.plan.metrics.transit_min,
                transit_distance_m=item.plan.metrics.transit_distance_m,
                budget_min=item.plan.budget.min_cny if item.plan.budget else None,
                budget_max=item.plan.budget.max_cny if item.plan.budget else None,
                budget_scope=item.plan.budget.scope if item.plan.budget else "per_person",
                place_count=item.plan.metrics.place_count,
                recommend_score=item.breakdown.total,
                score_breakdown=item.breakdown.as_dict(),
                best_for=list(_best_for(item)),
                highlights=_highlights(item),
                pros=_pros(item),
                cons=_cons(item),
                recommendation_reason=chosen.reason if chosen is not None else _reason(item),
                feasibility_report={
                    **item.report.as_dict(),
                    # 预算的"未知项"不在可行性报告里，但它同样必须能被前端读到：
                    # 只显示一个总额、把"有项目根本没算进去"藏起来，等于编造低预算。
                    "budget_unknown_items": list(item.plan.budget.unknown_items) if item.plan.budget else [],
                    "budget_estimated": item.plan.budget.estimated if item.plan.budget else True,
                },
                route_source=item.source,
                template_route_id=item.template_route_id,
                sort_order=order,
            )
            self.db.add(route)
            await self.db.flush()

            for seq, stop in enumerate(item.plan.stops):
                snapshot = snapshots.get(stop.place.id, _snapshot_of(stop.place))
                # 站点级警告只标它自己那一段："这一段耗时是估算的"是站点属性，
                # 把整条路线级的警告复制到每一站只会让界面警示满天飞。
                stop_warnings = (
                    ["ESTIMATED_TRANSIT"]
                    if stop.leg_to_next is not None and stop.leg_to_next.source == "estimated"
                    else []
                )
                self.db.add(
                    TripRouteStop(
                        trip_route_id=route.id,
                        seq=seq,
                        place_id=uuid.UUID(stop.place.id),
                        place_snapshot=snapshot,
                        arrive_time=_as_time(stop.arrive_min),
                        depart_time=_as_time(stop.depart_min),
                        stay_min=stop.stay_min,
                        transport_mode=stop.leg_to_next.mode if stop.leg_to_next else None,
                        transport_min=stop.leg_to_next.minutes if stop.leg_to_next else None,
                        transport_distance_m=stop.leg_to_next.distance_m if stop.leg_to_next else None,
                        transport_source=stop.leg_to_next.source if stop.leg_to_next else None,
                        why_recommended=_stop_why(stop.place, intent, self.scoring),
                        tips=None,
                        source_refs=snapshot.get("source_refs"),
                        warnings=stop_warnings,
                    )
                )
        await self.db.flush()

        # 外部调用的成本落库：成本报表读的是 cost_logs，不是日志里的一句声明。
        await self._persist_cost(ledger, request_id=request.id, trip_id=trip.id)

        ttl_hours = self.ttl.ttl_for("plan_cache")
        await CacheStore(self.db).put_plan(
            params_hash_value=fingerprint,
            kb_version=kb_version,
            trip_id=trip.id,
            ttl_hours=ttl_hours if ttl_hours is not None else 168,
        )

        llm_status = trace.status(
            cost_cny=ledger.spent_cny("llm"), cost_calibrated=_llm_calibrated(ledger)
        )
        log.info(
            "规划完成",
            extra={
                "event": "plan.completed",
                "context": {
                    "trip_id": str(trip.id),
                    "routes": len(selected),
                    "elapsed_ms": elapsed_ms,
                    "degraded": list(degraded),
                    # 将“用了/没用 LLM、用了几次、花了多少”写进日志：
                    # 没有这一行，线上就只能靠猜。
                    "llm": llm_status.as_dict(),
                },
            },
        )
        return PlanOutcome(
            request_id=request.id,
            trip_id=trip.id,
            route_count=len(selected),
            degraded_modes=tuple(degraded),
            cached=False,
            elapsed_ms=elapsed_ms,
            llm=llm_status,
        )

    async def _stop_snapshots(self, place_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
        """为站点补充快照里的来源署名（FR-11：来源可追溯是硬性要求）。"""
        if not place_ids:
            return {}
        try:
            ids = [uuid.UUID(pid) for pid in place_ids]
        except ValueError:  # pragma: no cover - 领域 id 一定来自数据库
            return {}
        rows = (await self.db.execute(select(Place).where(Place.id.in_(ids)))).scalars().all()
        out: dict[str, dict[str, Any]] = {}
        for row in rows:
            snapshot: dict[str, Any] = {
                "name": row.display_name,
                "category": row.category,
                "latitude": float(row.latitude),
                "longitude": float(row.longitude),
                "district": row.district,
                "tags": list(row.tags or []),
                "verification_status": row.verification_status,
                "price_min": None if row.price_min is None else str(row.price_min),
                "price_max": None if row.price_max is None else str(row.price_max),
            }
            if row.source_name or row.source_url:
                snapshot["source_refs"] = {
                    "name": row.source_name,
                    "url": row.source_url,
                    "updated_at": row.source_updated_at.isoformat() if row.source_updated_at else None,
                }
            out[str(row.id)] = snapshot
        return out

    async def _emit(self, sink: ProgressSink | None, progress: PlanProgress) -> None:
        if sink is not None:
            await sink(progress)


# ════════════════════════════════════════════════════════════════════════════
# 纯展示推导（全部来自数据，不编造）
# ════════════════════════════════════════════════════════════════════════════


def _jaccard(a: set[str], b: set[str]) -> float:
    union = len(a | b)
    return 0.0 if union == 0 else len(a & b) / union


def _as_time(minutes: int) -> dt_time:
    return dt_time(hour=(minutes // 60) % 24, minute=minutes % 60)


def _snapshot_of(place: DomainPlace) -> dict[str, Any]:
    return {
        "name": place.name,
        "category": place.category,
        "latitude": place.lat,
        "longitude": place.lng,
        "district": place.district,
        "tags": list(place.tags),
        "verification_status": place.verification_status,
        "price_min": None if place.price_min is None else str(place.price_min),
        "price_max": None if place.price_max is None else str(place.price_max),
    }


def _stop_why(place: DomainPlace, intent: Intent, scoring: ScoringConfig) -> str | None:
    """这一站为什么被推荐：命中阈值以上的偏好维度（数据推导，不编造理由）。"""
    active = intent.active_preferences
    if not active:
        return None
    threshold = scoring.formulas.preference.coverage_min_dim_score
    unknown = scoring.formulas.preference.unknown_score_default
    hit: list[str] = []
    for key, weight in active.items():
        dimension = scoring.preference_dimensions.get(key)
        if dimension is None or weight <= 0:
            continue
        if dimension_score(place, key, dimension, unknown) >= threshold:
            hit.append(str(dimension.label))
    return "·".join(hit) if hit else None


def _route_name(item: _ScoredPlan) -> str:
    names = [stop.place.name for stop in item.plan.stops[:3]]
    suffix = "…" if item.plan.metrics.place_count > 3 else ""
    prefix = ARCHETYPE_LABELS.get(item.archetype, item.archetype)
    return f"{prefix}路线：{' → '.join(names)}{suffix}"


def _one_liner(item: _ScoredPlan) -> str:
    metrics = item.plan.metrics
    hours = metrics.total_duration_min / 60
    return f"{metrics.place_count} 站 · 约 {hours:.1f} 小时 · 步行 {metrics.walking_m / 1000:.1f} km"


def _best_for(item: _ScoredPlan) -> tuple[str, ...]:
    """"适合谁"：由**这条路线自己的指标**推导，不写空话（PRD AC-6.3）。

    三套方案之间必须有差别，否则三个 Tab 换了个标签而已。
    这里只用可核验的数字（站数/步行/时长/是否模板）生成标签，
    不用"适合所有人"这类无法验证的说法。
    """
    metrics = item.plan.metrics
    tags: list[str] = [ARCHETYPE_LABELS.get(item.archetype, item.archetype)]
    if metrics.walking_m <= 4000:
        tags.append("不想多走路")
    if metrics.place_count >= 5:
        tags.append("想一次多看几个地方")
    if metrics.total_duration_min <= 300:
        tags.append("只有半天时间")
    if item.source == "template":
        tags.append("人工整理过的路线")
    return tuple(tags[:3])


def _highlights(item: _ScoredPlan) -> list[str]:
    ranked = sorted(
        item.plan.stops,
        key=lambda stop: (
            stop.place.popularity_score if stop.place.popularity_score is not None else -1.0
        ),
        reverse=True,
    )
    return [stop.place.name for stop in ranked[:3]]


def _pros(item: _ScoredPlan) -> list[str]:
    parts = {
        "preference": item.breakdown.preference,
        "efficiency": item.breakdown.efficiency,
        "time_fit": item.breakdown.time_fit,
        "popularity": item.breakdown.popularity,
        "budget_fit": item.breakdown.budget_fit,
        "walking_fit": item.breakdown.walking_fit,
        "place_relation": item.breakdown.place_relation,
    }
    top = sorted(parts.items(), key=lambda kv: kv[1], reverse=True)[:2]
    return [f"{DIMENSION_LABELS.get(key, key)}（{value:.2f}）" for key, value in top]


def _cons(item: _ScoredPlan) -> list[str]:
    return [warning.message for warning in item.report.warnings[:3]]


def _reason(item: _ScoredPlan) -> str:
    metrics = item.plan.metrics
    budget = item.plan.budget
    money = ""
    if budget is not None:
        money = f"，人均约 ¥{budget.min_cny:.0f}"
        if budget.unknown_items:
            money += "（不含未知票价项目）"
    return (
        f"共 {metrics.place_count} 站，总时长约 {metrics.total_duration_min} 分钟，"
        f"步行 {metrics.walking_m / 1000:.1f} 公里{money}。"
    )
