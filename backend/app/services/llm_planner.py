"""L7：LLM 参与规划的两个位置 —— 意图补全与方案叙事（PRD §15.1、FR-02、FR-06）。

★ 三条纪律（读代码前先读这三条）★

1. **模型不产生任何数字**。距离、时长、预算、可行性全部来自 ``app/domain`` 的纯函数。
   模型能改的只有两件事：*用户说了什么*（意图）与*怎么表达*（文案）。
   叙事里只要出现数字/金额/时长/距离，**整条丢弃**并退回模板文案 ——
   因为界面上"看起来由模型算出来的数"与"真的算出来的数"无法区分，
   宁可文案朴素一点，也不给用户一个没法核验的数字。
2. **失败一律降级**：无 Key（``NullLlmProvider``）/ 超时 / 5xx / 非 JSON /
   schema 不符 / 成本熔断 —— 全部退回规则引擎与模板文案，
   并把**真实原因**带给调用方（响应 ``meta.llm`` + 日志 + 成本账本）。
   "LLM 挂了导致规划失败"是本项目明确不允许出现的结果（PRD §15.5）。
3. **贵且慢，排在最后**：所有调用都经过检索链的 ``L5 缓存 → L7 模型``，
   命中缓存完全不花钱；prompt 版本进缓存键，改 prompt 等于换了输入。

调用方（``plan_service``）只看两个方法：``refine_intent`` 与 ``narrate``。
两者**都不会抛异常**（除非进程级故障），失败时返回原值并写进 trace。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core.config import TtlConfig
from app.core.logging import get_logger
from app.domain.models import BudgetSpec, Constraint, Intent, Pace, ParseResult
from app.providers.llm.base import LlmMessage, LlmProvider, LlmTier
from app.services.cache import CacheStore, llm_cache_key, sha256_hex
from app.services.cost import CostLedger
from app.services.retrieval_chain import RetrievalChain, RetrievalOutcome, RetrievalQuery
from app.services.retrieval_layers import KIND_LLM

__all__ = [
    "PROMPT_VERSION",
    "TASK_INTENT",
    "TASK_NARRATIVE",
    "LlmIntentPatch",
    "LlmNarrativeBundle",
    "LlmPlanner",
    "LlmRouteNarrative",
    "LlmStatus",
    "LlmTrace",
    "RouteNarrative",
    "RouteNarrativeInput",
    "merge_intent_patch",
    "usable_narratives",
]

log = get_logger("llm")

#: prompt 版本。改 prompt 文案/字段就必须改它，否则旧缓存答案会被继续复用。
PROMPT_VERSION = "2026.09.2"

TASK_INTENT = "intent_patch"
TASK_NARRATIVE = "route_narrative"

#: 意图补丁里最多接受的排除项数量（用户一次说不了那么多；超出的多半是模型在发散）
_MAX_EXCLUSIONS = 8
#: 叙事长度上限（超出直接判为不合格，不截断 —— 截断会切出半句话）
_NAME_MAX = 40
_REASON_MAX = 160

#: "像数字"的字符与单位。命中即整条丢弃（见模块 docstring 第 1 条）。
_DIGIT_CHARS = frozenset("0123456789０１２３４５６７８９¥￥$%")
# 注意“天”不在列：它在日常表达里出现频率太高（“今天”“一天”“白天”），
# 把它当计量单位会让正常文案被大量误杀；“2 天”这类写法依旧会被数字字符拦住。
_NUMBER_UNITS = ("元", "块", "分钟", "小时", "公里", "千米", "米", "站")


class LlmIntentPatch(BaseModel):
    """模型对自由文本的解析结果（结构化补丁）。

    ★ 刻意**只有可校验的字段** ★
    自由文本里"我想轻松点"这种感受无法结构化，就不设字段 ——
    模型的自由发挥越多，规则引擎的确定性就越少。
    所有数值都带上界，超界即 schema 不合法（而不是被悄悄夹到边界值）。
    """

    model_config = ConfigDict(extra="forbid")

    preferences: list[str] = Field(default_factory=list, max_length=12)
    pace: Pace | None = None
    days: int | None = Field(default=None, ge=1, le=14)
    people: int | None = Field(default=None, ge=1, le=20)
    budget_amount: Decimal | None = Field(default=None, ge=0, le=1_000_000)
    budget_scope: Literal["per_person", "total"] | None = None
    exclude: list[str] = Field(default_factory=list, max_length=_MAX_EXCLUSIONS)


class LlmRouteNarrative(BaseModel):
    """一条路线的文案。``index`` 用来对上调用方给出的路线顺序。"""

    model_config = ConfigDict(extra="forbid")

    index: int = Field(ge=0)
    name: str = Field(min_length=1, max_length=_NAME_MAX)
    reason: str = Field(min_length=1, max_length=_REASON_MAX)


class LlmNarrativeBundle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    routes: list[LlmRouteNarrative] = Field(default_factory=list, max_length=4)


@dataclass(frozen=True, slots=True)
class RouteNarrative:
    """通过校验、可安全展示的文案。"""

    name: str
    reason: str


@dataclass(frozen=True, slots=True)
class RouteNarrativeInput:
    """喂给模型的一条路线事实（**只有名字与标签，没有数字**）。

    为什么不把站数/时长/预算给模型：给了它就会忍不住写进文案里。
    数字由 ``_one_liner`` / ``recommendation_reason`` 的模板负责，模型不许越界。
    """

    index: int
    archetype: str
    stop_names: tuple[str, ...]
    pace: str
    preferences: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LlmStatus:
    """本次规划里 LLM 的**真实**使用情况（响应 / 日志 / 成本报表共用同一份事实）。

    没有它，"到底用没用大模型"就只剩一句无从核验的声明。
    """

    enabled: bool
    used: bool
    provider: str
    model: str | None
    prompt_version: str
    calls: int
    cache_hits: int
    tokens_in: int
    tokens_out: int
    cost_cny: str
    cost_calibrated: bool
    tasks: dict[str, str]
    fallback_reasons: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "used": self.used,
            "provider": self.provider,
            "model": self.model,
            "prompt_version": self.prompt_version,
            "calls": self.calls,
            "cache_hits": self.cache_hits,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "cost_cny": self.cost_cny,
            "cost_calibrated": self.cost_calibrated,
            "tasks": dict(self.tasks),
            "fallback_reasons": list(self.fallback_reasons),
        }


@dataclass
class LlmTrace:
    """规划过程中的 LLM 计数与降级原因（可变累加器，最后转成 :class:`LlmStatus`）。"""

    enabled: bool
    provider: str
    prompt_version: str = PROMPT_VERSION
    model: str | None = None
    calls: int = 0
    cache_hits: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    #: task → 本次该任务的归属（llm / cache / rule）
    tasks: dict[str, str] = field(default_factory=dict)
    fallback_reasons: list[str] = field(default_factory=list)

    @property
    def used(self) -> bool:
        """是否真的用到了模型：命中缓存也算"用了"（它同样是模型的产出）。"""
        return self.calls > 0 or self.cache_hits > 0

    def note_task(self, task: str, source: str, reason: str | None = None) -> None:
        self.tasks[task] = source
        if reason is not None and reason not in self.fallback_reasons:
            self.fallback_reasons.append(reason)

    def status(self, *, cost_cny: Decimal, cost_calibrated: bool) -> LlmStatus:
        return LlmStatus(
            enabled=self.enabled,
            used=self.used,
            provider=self.provider,
            model=self.model,
            prompt_version=self.prompt_version,
            calls=self.calls,
            cache_hits=self.cache_hits,
            tokens_in=self.tokens_in,
            tokens_out=self.tokens_out,
            cost_cny=str(cost_cny),
            cost_calibrated=cost_calibrated,
            tasks=dict(self.tasks),
            fallback_reasons=list(self.fallback_reasons),
        )


# ════════════════════════════════════════════════════════════════════════════
# 纯逻辑：prompt 构造、合并、过滤（可脱离数据库与网络单测）
# ════════════════════════════════════════════════════════════════════════════


_INTENT_SYSTEM = (
    "你是旅行需求解析器。只输出一个 JSON 对象，不要输出解释、Markdown 或代码块标记。\n"
    "规则：\n"
    "1. 只提取用户**明确说过**的信息；没说的一律 null 或空数组，不要猜测、不要补全、不要举例。\n"
    "2. preferences 只能从给定的 allowed_preferences 里取值（英文 key）。\n"
    "3. budget_amount 是人民币元的数字；budget_scope 取 per_person（人均）或 total（总预算）。\n"
    "4. exclude 只放用户明确说不要去的地点名，不要编造地点。\n"
    "5. 拿不准就留 null —— 交给规则引擎处理比编一个值更安全。"
)

_INTENT_SCHEMA_HINT: dict[str, Any] = {
    "preferences": ["food", "photo"],
    "pace": "relaxed | balanced | packed | null",
    "days": "整数 1-14 或 null",
    "people": "整数 1-20 或 null",
    "budget_amount": "数字或 null",
    "budget_scope": "per_person | total | null",
    "exclude": ["地点名"],
}


def intent_messages(
    *,
    free_text: str,
    unparsed: Sequence[str],
    form_summary: Mapping[str, Any],
    allowed_preferences: Sequence[str],
) -> list[LlmMessage]:
    """构造意图解析的 messages（纯函数，便于单测与缓存键计算）。"""
    payload = {
        "free_text": free_text,
        "unparsed_fragments": list(unparsed),
        "form_known": dict(form_summary),
        "allowed_preferences": sorted(allowed_preferences),
        "output_format_example": _INTENT_SCHEMA_HINT,
    }
    return [
        LlmMessage(role="system", content=_INTENT_SYSTEM),
        LlmMessage(role="user", content=json.dumps(payload, ensure_ascii=False)),
    ]


_NARRATIVE_SYSTEM = (
    "你是旅行路线文案编辑。只输出一个 JSON 对象，不要输出解释或 Markdown。\n"
    "规则：\n"
    "1. **禁止出现任何数字、金额、时长、距离、站数**（这些由系统计算并展示，写错会误导用户）。\n"
    "2. 只能使用给定站点名，不得引入新地点，不得承诺营业时间、票价、天气。\n"
    "3. name 不超过 20 字，reason 不超过 80 字；语言平实，不堆形容词。\n"
    "4. index 必须与输入的路线序号一致，每条路线恰好一条文案。"
)


def narrative_messages(routes: Sequence[RouteNarrativeInput]) -> list[LlmMessage]:
    """构造方案叙事的 messages（纯函数）。"""
    payload = {
        "routes": [
            {
                "index": route.index,
                "archetype": route.archetype,
                "stops": list(route.stop_names),
                "pace": route.pace,
                "preferences": list(route.preferences),
            }
            for route in routes
        ],
        "output_format_example": {
            "routes": [{"index": 0, "name": "文案", "reason": "文案"}]
        },
    }
    return [
        LlmMessage(role="system", content=_NARRATIVE_SYSTEM),
        LlmMessage(role="user", content=json.dumps(payload, ensure_ascii=False)),
    ]


def has_number_like(text: str) -> bool:
    """文案里是否出现了"像数字的东西"（数字字符或计量单位）。

    ``一``/``两`` 这类中文数量词**不**拦截：``一路走下来`` 是正常表达，
    把它们也拦掉会让叙事几乎全部退化成模板文案，得不偿失。
    真正的边界是"可被误读为行程事实的数字"：数字字符与元/分钟/公里/站等单位。
    """
    if any(char in _DIGIT_CHARS for char in text):
        return True
    return any(unit in text for unit in _NUMBER_UNITS)


def merge_intent_patch(
    parsed: ParseResult,
    patch: LlmIntentPatch,
    *,
    allowed_preferences: Sequence[str],
) -> tuple[ParseResult, tuple[str, ...]]:
    """把模型补丁合并到规则结果上（纯函数）。

    语义：**规则结果先，模型补丁后**。规则引擎已经覆盖的片段（例如"人均 300"）
    若模型又给出不同的数字，以模型为准 —— 因为模型看的是一样的原文，
    而它出错的代价被 schema 与上界限制住了；反过来（规则覆盖模型）则会让
    "规则没解析出来的那句"永远没人管。

    返回 ``(新 ParseResult, 实际生效的字段名)``；没有字段生效时返回原值，
    调用方据此保持 ``parse_source="rule"``（不假装用了模型）。
    """
    intent = parsed.intent
    allowed = set(allowed_preferences)

    prefs = dict(intent.preferences)
    applied: list[str] = []
    accepted = [key for key in patch.preferences if key in allowed]
    if accepted:
        changed = False
        for key in accepted:
            if prefs.get(key, 0.0) <= 0.0:
                prefs[key] = 1.0
                changed = True
        if changed:
            applied.append("preferences")

    pace: Pace = patch.pace if patch.pace is not None else intent.pace
    if patch.pace is not None and patch.pace != intent.pace:
        applied.append("pace")

    days = intent.days
    if patch.days is not None and patch.days != intent.days:
        days = patch.days
        applied.append("days")

    people = intent.people
    if patch.people is not None and patch.people != intent.people:
        people = patch.people
        applied.append("people")

    budget = intent.budget
    if patch.budget_amount is not None or patch.budget_scope is not None:
        budget = BudgetSpec(
            amount=patch.budget_amount if patch.budget_amount is not None else intent.budget.amount,
            scope=patch.budget_scope if patch.budget_scope is not None else intent.budget.scope,
            currency=intent.budget.currency,
        )
        applied.append("budget")

    # ★ 约束类型必须与规则引擎完全一致（``exclude_place``）★
    # 领域层的 ``select_candidates`` 只认 ``exclude_place`` / ``exclude_place_id``；
    # 在这里新造一个 ``"exclude"`` 看着更“直白”，实际会被**静默忽略** ——
    # 用户说“别去博物馆”，界面照旧安排博物馆，而所有测试仍然是绿的。
    existing: set[str] = set()
    for constraint in parsed.constraints:
        if constraint.type.startswith("exclude") and isinstance(constraint.value, str):
            existing.add(constraint.value.strip().lower())
    extra: list[Constraint] = []
    for name in patch.exclude:
        clean = name.strip()
        if not clean or clean.lower() in existing:
            continue
        existing.add(clean.lower())
        extra.append(Constraint(type="exclude_place", value=clean, raw=clean, source="llm"))
    if extra:
        applied.append("exclude")

    if not applied:
        return parsed, ()

    merged_intent = Intent(
        city=intent.city,
        days=days,
        people=people,
        preferences=prefs,
        pace=pace,
        budget=budget,
        start_min=intent.start_min,
        end_min=intent.end_min,
        travel_date=intent.travel_date,
        weather_sensitive=intent.weather_sensitive,
    )
    merged = ParseResult(
        intent=merged_intent,
        constraints=(*parsed.constraints, *extra),
        applied_rules=(*parsed.applied_rules, *(f"llm:{name}" for name in applied)),
        # 交给模型兜底的那几段已经被处理过了，不再重复抛给下游
        unparsed=(),
        parse_source="hybrid",
    )
    return merged, tuple(applied)


def usable_narratives(
    bundle: LlmNarrativeBundle, *, route_count: int
) -> tuple[dict[int, RouteNarrative], list[str]]:
    """过滤模型文案，返回 ``(可用文案, 丢弃原因)``（纯函数）。

    三类别丢弃，全部**整条丢弃而不是修补**：

    - 索引越界（模型数错路线数）→ 该条丢弃；
    - 含数字/金额/时长/距离 → 丢弃（见 :func:`has_number_like`）；
    - 同一 index 重复出现 → 第一条生效，后续丢弃（避免"随机挑一条"的不确定性）。
    """
    usable: dict[int, RouteNarrative] = {}
    rejected: list[str] = []
    for item in bundle.routes:
        if item.index >= route_count or item.index < 0:
            rejected.append(f"index={item.index} 越界")
            continue
        if item.index in usable:
            rejected.append(f"index={item.index} 重复")
            continue
        text = f"{item.name}\n{item.reason}"
        if has_number_like(text):
            rejected.append(f"index={item.index} 含数字或单位")
            continue
        usable[item.index] = RouteNarrative(name=item.name.strip(), reason=item.reason.strip())
    return usable, rejected


# ════════════════════════════════════════════════════════════════════════════
# 编排：走检索链（L5 缓存 → L7 模型），失败即降级
# ════════════════════════════════════════════════════════════════════════════


@dataclass
class LlmPlanner:
    """把"调模型"这件事收敛到一个类里：缓存、成本、重试、降级、追踪都在这里。"""

    chain: RetrievalChain
    store: CacheStore
    ledger: CostLedger
    provider: LlmProvider
    trace: LlmTrace
    ttl: TtlConfig
    #: 单次输出的 token 上限（来自 limits.providers.llm_max_output_tokens 的收敛值）
    max_output_tokens: int = 1024
    #: schema 不符时的重试次数（limits.yaml: llm_max_retries）
    schema_retries: int = 1

    async def refine_intent(
        self,
        parsed: ParseResult,
        *,
        free_text: str,
        form_summary: Mapping[str, Any],
        allowed_preferences: Sequence[str],
    ) -> ParseResult:
        """用模型补全规则引擎没解析出来的部分；任何失败都返回原 ``parsed``。"""
        if not self.trace.enabled or not free_text.strip():
            # 没配 Key 不算“降级失败”：那是**已知且允许**的运行状态，
            # 由 status.enabled=False 与 degraded_modes 表达，
            # fallback_reasons 只记录“配了却没用上”的原因，否则这个词会被稀释。
            self.trace.note_task(TASK_INTENT, "rule")
            return parsed

        messages = intent_messages(
            free_text=free_text,
            unparsed=parsed.unparsed,
            form_summary=form_summary,
            allowed_preferences=allowed_preferences,
        )
        patch = await self._complete_json(
            task=TASK_INTENT,
            messages=messages,
            schema=LlmIntentPatch,
            ttl_kind="llm_structured",
            temperature=0.0,
            max_output_tokens=512,
        )
        if patch is None:
            return parsed

        merged, applied = merge_intent_patch(parsed, patch, allowed_preferences=allowed_preferences)
        if not applied:
            # 模型回答了，但没识别出可应用的信息 —— 这不算降级，如实记为 llm。
            self.trace.note_task(TASK_INTENT, "llm")
            return parsed
        self.trace.note_task(TASK_INTENT, "llm")
        log.info(
            "LLM 补全意图",
            extra={
                "event": "plan.llm_intent",
                "context": {"applied": list(applied), "parse_source": merged.parse_source},
            },
        )
        return merged

    async def narrate(
        self, routes: Sequence[RouteNarrativeInput]
    ) -> dict[int, RouteNarrative]:
        """用模型写方案文案；失败或文案不合格时返回空 dict（调用方用模板文案兜底）。"""
        if not self.trace.enabled or not routes:
            self.trace.note_task(TASK_NARRATIVE, "rule")
            return {}

        bundle = await self._complete_json(
            task=TASK_NARRATIVE,
            messages=narrative_messages(routes),
            schema=LlmNarrativeBundle,
            ttl_kind="llm_narrative",
            temperature=0.3,
            max_output_tokens=1024,
        )
        if bundle is None:
            return {}

        usable, rejected = usable_narratives(bundle, route_count=len(routes))
        if rejected:
            log.warning(
                "LLM 文案被丢弃（数字/越界/重复）",
                extra={
                    "event": "plan.llm_narrative_rejected",
                    "context": {"rejected": rejected, "usable": len(usable)},
                },
            )
        if not usable:
            self.trace.note_task(TASK_NARRATIVE, "rule", "PROVIDER_INVALID_RESPONSE（文案不合格，已用模板）")
            return {}
        self.trace.note_task(TASK_NARRATIVE, "llm")
        return usable

    # ── 内部：一次强校验的 JSON 调用 ──

    async def _complete_json[TModel: BaseModel](
        self,
        *,
        task: str,
        messages: Sequence[LlmMessage],
        schema: type[TModel],
        ttl_kind: str,
        temperature: float,
        max_output_tokens: int,
    ) -> TModel | None:
        tier: LlmTier = "fast"
        model = self.provider.model_for(tier)
        prompt_text = "\n\n".join(message.content for message in messages)
        cache_key = llm_cache_key(
            tier=tier,
            model=model,
            prompt_version=self.trace.prompt_version,
            prompt_text=prompt_text,
            schema_hash=sha256_hex(json.dumps(schema.model_json_schema(), sort_keys=True)),
            temperature=temperature,
        )
        limit = min(max_output_tokens, self.max_output_tokens)
        attempts = max(1, self.schema_retries + 1)

        for attempt in range(attempts):
            try:
                outcome = await self.chain.resolve(
                    RetrievalQuery(
                        kind=KIND_LLM,
                        key=cache_key,
                        payload={
                            "cache_key": cache_key,
                            "messages": messages,
                            "tier": tier,
                            "temperature": temperature,
                            "max_output_tokens": limit,
                            "json_mode": True,
                            "task": task,
                        },
                    )
                )
            except Exception as exc:
                # 检索链只兜 ProviderError / CostBreakerOpen；其余意外（例如连接层
                # 抛出的裸异常）必须在这里被拦住 —— "LLM 出问题导致规划失败"
                # 是明确不允许的结果（PRD §15.5）。
                reason = f"{type(exc).__name__}（已降级到规则引擎/模板文案）"
                self.trace.note_task(task, "rule", reason)
                log.warning(
                    "LLM 调用抛出意外异常，降级到规则/模板",
                    exc_info=True,
                    extra={"event": "plan.llm_fallback", "code": reason, "context": {"task": task}},
                )
                return None
            if not outcome.resolved:
                reason = _unavailable_reason(outcome)
                self.trace.note_task(task, "rule", reason)
                log.warning(
                    "LLM 不可用，降级到规则/模板",
                    extra={
                        "event": "plan.llm_fallback",
                        "code": reason,
                        "context": {"task": task, "failed_layers": list(outcome.failed_layers)},
                    },
                )
                return None

            cached = isinstance(outcome.value, Mapping)
            text, model_name = _text_of(outcome.value)
            if not cached:
                self.trace.calls += 1
                self.trace.model = model_name or self.trace.model
                self.trace.tokens_in += _usage_int(outcome.value, "tokens_in")
                self.trace.tokens_out += _usage_int(outcome.value, "tokens_out")

            try:
                parsed = schema.model_validate(_json_object(text))
            except (ValidationError, ValueError) as exc:
                log.warning(
                    "LLM 输出不符合 schema",
                    extra={
                        "event": "plan.llm_invalid_response",
                        "context": {"task": task, "attempt": attempt + 1, "error": str(exc)[:200]},
                    },
                )
                continue

            if cached:
                self.trace.cache_hits += 1
                # 缓存命中也要记账（金额 0）：否则"命中了多少"无法从成本日志里算出来。
                self.ledger.record_cache_hit("llm", self.provider.name, operation=task)
                return parsed

            await self._write_cache(
                cache_key=cache_key,
                task=task,
                tier=tier,
                model=model_name or model,
                prompt_text=prompt_text,
                schema=schema,
                text=text,
                ttl_kind=ttl_kind,
                response=outcome.value,
            )
            return parsed

        self.trace.note_task(
            task,
            "rule",
            f"PROVIDER_INVALID_RESPONSE（连续 {attempts} 次输出不符合 schema，已用规则/模板）",
        )
        return None

    async def _write_cache[TModel: BaseModel](
        self,
        *,
        cache_key: str,
        task: str,
        tier: str,
        model: str,
        prompt_text: str,
        schema: type[TModel],
        text: str,
        ttl_kind: str,
        response: Any,
    ) -> None:
        """把成功的模型输出写进 LLM 缓存；写失败**不影响本次结果**（只是下次再花钱）。"""
        try:
            await self.store.put_llm(
                cache_key=cache_key,
                tier=tier,
                model=model,
                prompt_hash=sha256_hex(prompt_text),
                task=task,
                response={"text": text, "model": model, "schema_hash": _schema_hash(schema)},
                ttl_hours=self.ttl.ttl_for(ttl_kind),
                tokens_in=_usage_int(response, "tokens_in"),
                tokens_out=_usage_int(response, "tokens_out"),
                tokens_cached=_usage_int(response, "tokens_cached"),
                cost_cny=Decimal("0"),
            )
        except Exception:  # pragma: no cover - 缓存是加速器，坏了不该影响规划
            log.warning(
                "写入 LLM 缓存失败（不影响本次结果）",
                exc_info=True,
                extra={"event": "plan.llm_cache_write_failed", "context": {"task": task}},
            )


def _schema_hash[TModel: BaseModel](schema: type[TModel]) -> str:
    return sha256_hex(json.dumps(schema.model_json_schema(), sort_keys=True))


def _json_object(text: str) -> dict[str, Any]:
    """把模型输出解析成 JSON 对象；不是对象就直接失败（不尝试"从散文里抠 JSON"）。"""
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("模型输出的不是 JSON 对象")
    return data


def _text_of(value: Any) -> tuple[str, str]:
    """从 LlmResponse 或缓存 dict 里取出 ``(文本, 模型名)``。"""
    if isinstance(value, Mapping):
        return str(value.get("text", "")), str(value.get("model", ""))
    return str(getattr(value, "text", "")), str(getattr(value, "model", ""))


def _usage_int(response: Any, field_name: str) -> int:
    """从 ``LlmResponse.usage`` 取 token 数；缓存命中/用量未知时记 0（金额另有标记）。"""
    usage = getattr(response, "usage", None)
    value = getattr(usage, field_name, 0) if usage is not None else 0
    return int(value) if isinstance(value, int) else 0


def _unavailable_reason(outcome: RetrievalOutcome) -> str:
    """把"链上没人接"翻译成人能读懂的原因（区分"没配 Key"与"调用失败"）。"""
    codes = [attempt.error_code for attempt in outcome.attempts if attempt.error_code]
    if codes:
        return f"{codes[-1]}（已降级到规则引擎/模板文案）"
    if outcome.failed_layers:
        return f"{outcome.failed_layers[-1]}（已降级到规则引擎/模板文案）"
    return "LLM 未参与本次调用（已用规则引擎/模板文案）"
