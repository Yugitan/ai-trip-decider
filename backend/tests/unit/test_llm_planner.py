"""LLM 参与规划这一层的单元测试（纯逻辑 + 降级路径，零网络、零数据库）。

这里守三件事，每一件都对应 ``llm_planner`` 模块 docstring 里的一条纪律：

1. **模型不许产生数字**：意图合并只认白名单字段与绑定上界；叙事里出现数字/单位即整条丢弃。
2. **失败必须降级**：无 Key、返回非 JSON、schema 不符 —— 一律回到规则引擎与模板文案，
   并且降级原因要能被调用方读到（"降级了但没人知道"比直接报错更坑）。
3. **命中缓存不重复付费**：第二次同样的请求必须走 L5 缓存（金额 0 但留记录），
   不能又调一次模型。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, cast

import pytest

from app.core.config import get_limits_config, get_pricing_config, get_ttl_config
from app.core.errors import ErrorCode, ProviderError
from app.domain.models import BudgetSpec, Constraint, DayPlan, Intent, ParseResult
from app.providers.base import ProviderHealth
from app.providers.llm.base import LlmMessage, LlmResponse, LlmTier, LlmUsage
from app.providers.llm.null import NullLlmProvider
from app.services.cache import CacheStore
from app.services.cost import CostLedger, PriceBook
from app.services.llm_planner import (
    TASK_INTENT,
    TASK_NARRATIVE,
    LlmIntentPatch,
    LlmNarrativeBundle,
    LlmPlanner,
    LlmRouteNarrative,
    LlmTrace,
    RouteNarrativeInput,
    has_number_like,
    intent_messages,
    merge_intent_patch,
    narrative_messages,
    usable_narratives,
)
from app.services.retrieval_chain import RetrievalChain
from app.services.retrieval_layers import CacheLayer, LlmLayer

pytestmark = pytest.mark.unit

_PREFS = ("food", "photo", "museum", "nature", "night_view")


# ── 替身 ────────────────────────────────────────────────────────────────────


@dataclass
class _ScriptedLlm:
    """按脚本依次返回响应/抛错，用来驱动"模型抽风"的各种分支。"""

    script: list[LlmResponse | Exception]
    calls: int = 0
    name: str = "deepseek"

    def model_for(self, tier: LlmTier) -> str:
        return f"scripted-{tier}"

    def health(self) -> ProviderHealth:
        return ProviderHealth(name=self.name, available=True, detail="已配置 API Key")

    async def complete(
        self,
        messages: Sequence[LlmMessage],
        *,
        tier: LlmTier,
        max_output_tokens: int,
        temperature: float = 0.2,
        json_mode: bool = False,
    ) -> LlmResponse:
        item = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        if isinstance(item, Exception):
            raise item
        return item


@dataclass
class _FakeStore:
    """内存版 LLM 缓存（真库行为由集成测试覆盖）。"""

    writes: list[dict[str, Any]] = field(default_factory=list)
    hits: dict[str, dict[str, Any]] = field(default_factory=dict)

    async def get_llm(self, cache_key: str) -> dict[str, Any] | None:
        return self.hits.get(cache_key)

    async def put_llm(self, **kwargs: Any) -> None:
        self.writes.append(kwargs)
        cache_key = str(kwargs.get("cache_key", ""))
        response = kwargs.get("response")
        if cache_key and isinstance(response, dict):
            self.hits[cache_key] = response


def _response(text: str) -> LlmResponse:
    return LlmResponse(
        text=text,
        model="deepseek-chat",
        provider="deepseek",
        usage=LlmUsage(tokens_in=12, tokens_out=5),
    )


def _ledger() -> CostLedger:
    limits = get_limits_config()
    return CostLedger(
        price_book=PriceBook(get_pricing_config()),
        breaker=limits.cost.circuit_breaker,
        providers=limits.providers,
    )


def _planner(
    provider: Any, *, store: _FakeStore | None = None, enabled: bool = True
) -> tuple[LlmPlanner, LlmTrace, CostLedger, _FakeStore]:
    ledger = _ledger()
    fake_store = store if store is not None else _FakeStore()
    trace = LlmTrace(enabled=enabled, provider=getattr(provider, "name", "disabled"))
    chain = RetrievalChain(
        layers=[
            CacheLayer(store=cast(CacheStore, fake_store)),
            LlmLayer(provider=provider, ledger=ledger),
        ]
    )
    planner = LlmPlanner(
        chain=chain,
        store=cast(CacheStore, fake_store),
        ledger=ledger,
        provider=provider,
        trace=trace,
        ttl=get_ttl_config(),
    )
    return planner, trace, ledger, fake_store


def _parsed() -> ParseResult:
    return ParseResult(
        intent=Intent(
            city="guangzhou",
            days=1,
            people=2,
            preferences={"food": 1.0},
            budget=BudgetSpec(amount=Decimal("300"), scope="per_person"),
        ),
        constraints=(),
        applied_rules=("pace",),
        unparsed=("想看夜景",),
        parse_source="rule",
    )


def _narrative_inputs() -> list[RouteNarrativeInput]:
    return [
        RouteNarrativeInput(
            index=0,
            archetype="relaxed",
            stop_names=("北京路商业步行街", "点都德"),
            pace="relaxed",
            preferences=("food",),
        )
    ]


async def _refine(planner: LlmPlanner) -> ParseResult:
    return await planner.refine_intent(
        _parsed(), free_text="想看夜景", form_summary={}, allowed_preferences=_PREFS
    )


# ── 纯逻辑：prompt ──────────────────────────────────────────────────────────


def test_intent_prompt_carries_free_text_and_allowed_keys() -> None:
    messages = intent_messages(
        free_text="想吃辣的，别去广州塔",
        unparsed=("想看夜景",),
        form_summary={"days": 1},
        allowed_preferences=_PREFS,
    )
    assert messages[0].role == "system"
    assert "只输出一个 JSON 对象" in messages[0].content
    user = messages[1].content
    assert "想吃辣的，别去广州塔" in user
    assert "想看夜景" in user
    assert "museum" in user


def test_narrative_prompt_forbids_numbers_and_new_places() -> None:
    messages = narrative_messages(_narrative_inputs())
    system = messages[0].content
    assert "禁止出现任何数字" in system
    assert "只能使用给定站点名" in system
    # 站点名照原样带给模型，避免它"改写"成别的地点
    assert "北京路商业步行街" in messages[1].content


# ── 纯逻辑：数字守卫 ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "blocked"),
    [
        ("适合轻松逛的一天", False),
        ("一路走下来很有老城味道", False),
        ("非常适合拍照的街区", False),
        ("步行约 2 公里", True),
        ("人均 80 元", True),
        # “站”是计量单位：文案里出现它就在暗示行程事实，交给代码说更安全
        ("一共三站", True),
        ("票价 ¥50", True),
        ("建议停留 40分钟", True),
    ],
)
def test_has_number_like(text: str, blocked: bool) -> None:
    assert has_number_like(text) is blocked


def test_usable_narratives_drops_bad_entries_and_keeps_indices() -> None:
    bundle = LlmNarrativeBundle(
        routes=[
            LlmRouteNarrative(index=0, name="老城散步", reason="适合慢慢逛"),
            LlmRouteNarrative(index=9, name="越界", reason="路线没这么多条"),
            LlmRouteNarrative(index=0, name="重复", reason="同一个索引第二条"),
            LlmRouteNarrative(index=1, name="含数字", reason="步行约 2 公里"),
        ]
    )
    usable, rejected = usable_narratives(bundle, route_count=3)
    assert set(usable) == {0}
    assert usable[0].name == "老城散步"
    assert len(rejected) == 3


# ── 纯逻辑：意图合并 ────────────────────────────────────────────────────────


def test_merge_intent_patch_applies_whitelisted_fields() -> None:
    merged, applied = merge_intent_patch(
        _parsed(),
        LlmIntentPatch(
            preferences=["photo", "not_a_real_dim"],
            pace="packed",
            days=2,
            people=4,
            budget_amount=Decimal("500"),
            budget_scope="total",
            exclude=["广州塔"],
        ),
        allowed_preferences=_PREFS,
    )
    assert set(applied) == {"preferences", "pace", "days", "people", "budget", "exclude"}
    assert merged.parse_source == "hybrid"
    assert merged.intent.preferences["photo"] == 1.0
    assert "not_a_real_dim" not in merged.intent.preferences
    assert merged.intent.pace == "packed"
    assert merged.intent.days == 2
    assert merged.intent.people == 4
    assert merged.intent.budget.amount == Decimal("500")
    assert merged.intent.budget.scope == "total"
    assert [c.value for c in merged.constraints] == ["广州塔"]
    assert merged.constraints[0].source == "llm"
    # ★ 类型必须是 select_candidates 真正消费的那个 ★
    # 新造一个 "exclude" 会被静默忽略：用户说“别去”，行程里还安排着，测试却是绿的。
    assert merged.constraints[0].type == "exclude_place"
    # 已交给模型处理的片段不再重复抛给下游
    assert merged.unparsed == ()


def test_merge_intent_patch_preserves_fields_the_model_does_not_own() -> None:
    """★ 逐字段重建的地方，漏一个字段就等于「模型一补全就把它重置」★

    ``day_span`` 与 ``day_plans`` 不属于模型的职权（它只改偏好/天数/预算/排除），
    但它们必须原样活过这次合并 —— 它们都曾经被这个函数静默丢掉：
    用户选了「半天」、逐天选了主题，一句补充要求触发模型补全，就变回默认值，
    而修改后的那一版看起来完全正常。
    """
    parsed = ParseResult(
        intent=Intent(
            day_span="half_day",
            day_plans=(DayPlan(pace="packed", theme="food"), DayPlan(pace="relaxed")),
        ),
        constraints=(),
        applied_rules=("day_span:half_day",),
        unparsed=("想看夜景",),
        parse_source="rule",
    )
    merged, applied = merge_intent_patch(
        parsed,
        LlmIntentPatch(preferences=["night_view"]),
        allowed_preferences=_PREFS,
    )

    assert applied == ("preferences",)
    assert merged.intent.day_span == "half_day"
    assert merged.intent.day_plans == (
        DayPlan(pace="packed", theme="food"),
        DayPlan(pace="relaxed"),
    )


def test_merge_intent_patch_pace_applies_to_every_day() -> None:
    """模型说"轻松点"与规则引擎说"轻松点"必须得到同一个结果：**整趟**每一无。

    否则同一句话会因"哪条路解析到"而产生两套互不相同的行程。"""
    parsed = ParseResult(
        intent=Intent(
            pace="relaxed",
            day_plans=(DayPlan(pace="relaxed", theme="food"), DayPlan(pace="relaxed", theme="culture")),
        ),
        constraints=(),
        applied_rules=(),
        unparsed=("紧凑一点",),
        parse_source="rule",
    )
    merged, applied = merge_intent_patch(
        parsed,
        LlmIntentPatch(pace="packed"),
        allowed_preferences=_PREFS,
    )

    assert applied == ("pace",)
    assert merged.intent.pace == "packed"
    assert [plan.pace for plan in merged.intent.day_plans] == ["packed", "packed"]
    # 主题不是节奏：它不能被顺手改掉
    assert [plan.theme for plan in merged.intent.day_plans] == ["food", "culture"]


def test_merge_intent_patch_keeps_rule_result_when_nothing_applies() -> None:
    original = _parsed()
    merged, applied = merge_intent_patch(
        original,
        LlmIntentPatch(preferences=["not_a_real_dim"]),
        allowed_preferences=_PREFS,
    )
    assert applied == ()
    # 没有字段生效时**不假装**用了模型
    assert merged is original
    assert merged.parse_source == "rule"


def test_merge_intent_patch_does_not_duplicate_existing_exclusion() -> None:
    """规则引擎已经排除过的地点，模型再说一遍不应变成两条约束。

    实测踩到过：规则引擎产出 ``exclude_place``，模型补丁又加一条 ``exclude``，
    同一个“博物馆”在约束列表里出现两次，之后任何按约束计数的逻辑都会被误导。
    """
    parsed = ParseResult(
        intent=Intent(city="guangzhou"),
        constraints=(Constraint(type="exclude_place", value="广州塔", raw="广州塔"),),
    )
    merged, applied = merge_intent_patch(
        parsed,
        LlmIntentPatch(exclude=["广州塔", "  "]),
        allowed_preferences=_PREFS,
    )
    assert applied == ()
    assert len(merged.constraints) == 1


def test_intent_patch_rejects_out_of_range_numbers() -> None:
    """上界是 schema 的一部分：超出就整条不合法，而不是被悄悄夹到边界。"""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        LlmIntentPatch(days=99)
    with pytest.raises(ValidationError):
        LlmIntentPatch(people=0)


# ── 降级路径 ────────────────────────────────────────────────────────────────


async def test_no_key_degrades_to_rules_without_calling_model() -> None:
    planner, trace, ledger, _ = _planner(NullLlmProvider(), enabled=False)

    refined = await _refine(planner)
    narrative = await planner.narrate(_narrative_inputs())

    assert refined.parse_source == "rule"
    assert narrative == {}
    assert trace.calls == 0
    assert trace.used is False
    assert trace.tasks[TASK_INTENT] == "rule"
    assert trace.tasks[TASK_NARRATIVE] == "rule"
    assert ledger.entries == []


async def test_provider_error_degrades_and_records_reason() -> None:
    provider = _ScriptedLlm(
        script=[
            ProviderError("deepseek", "complete", kind=ErrorCode.PROVIDER_TIMEOUT, message="超时")
        ]
    )
    planner, trace, _, _ = _planner(provider)

    refined = await _refine(planner)

    assert refined.parse_source == "rule", "调用失败必须原样返回规则结果"
    assert trace.tasks[TASK_INTENT] == "rule"
    assert any("PROVIDER_TIMEOUT" in reason for reason in trace.fallback_reasons)


async def test_non_json_output_is_not_salvaged_by_guessing() -> None:
    """模型回一段散文时：不"从散文里抠 JSON"，重试一次后降级。"""
    provider = _ScriptedLlm(script=[_response("我觉得应该多安排美食"), _response("仍然不是 JSON")])
    planner, trace, _, _ = _planner(provider)

    refined = await _refine(planner)

    assert provider.calls == 2
    assert refined.parse_source == "rule"
    assert trace.tasks[TASK_INTENT] == "rule"
    assert any("PROVIDER_INVALID_RESPONSE" in reason for reason in trace.fallback_reasons)


async def test_patch_without_usable_fields_does_not_claim_llm_parsing() -> None:
    provider = _ScriptedLlm(script=[_response('{"preferences": ["not_a_real_dim"]}')])
    planner, trace, _, store = _planner(provider)

    refined = await _refine(planner)

    assert provider.calls == 1
    assert refined.parse_source == "rule", "没有字段生效就不该声称 llm/hybrid"
    assert trace.tasks[TASK_INTENT] == "llm", "但确实调用了模型，如实记录"
    assert store.writes, "成功返回的 JSON 仍应写入缓存"


async def test_schema_violation_after_retries_falls_back() -> None:
    provider = _ScriptedLlm(script=[_response('{"days": 99}'), _response('{"days": 99}')])
    planner, trace, _, _ = _planner(provider)

    refined = await _refine(planner)

    assert provider.calls == 2, "schema 不符要重试一次（limits: llm_max_retries=1）"
    assert refined.parse_source == "rule"
    assert trace.tasks[TASK_INTENT] == "rule"
    assert any("PROVIDER_INVALID_RESPONSE" in reason for reason in trace.fallback_reasons)


async def test_successful_patch_uses_model_and_writes_cache() -> None:
    provider = _ScriptedLlm(
        script=[_response('{"preferences": ["night_view"], "exclude": []}')]
    )
    planner, trace, _, store = _planner(provider)

    refined = await _refine(planner)

    assert refined.parse_source == "hybrid"
    assert refined.intent.preferences["night_view"] == 1.0
    assert trace.tasks[TASK_INTENT] == "llm"
    assert trace.calls == 1
    assert (trace.tokens_in, trace.tokens_out) == (12, 5)
    assert trace.used is True
    assert len(store.writes) == 1
    written = store.writes[0]
    assert written["task"] == TASK_INTENT
    assert written["response"]["text"].startswith("{")
    assert written["ttl_hours"] is None, "结构化输出靠 prompt 版本失效，不按小时过期"


async def test_second_identical_request_serves_from_cache_without_paying() -> None:
    """同一个 prompt 再来一次：必须命中 L5，不再调模型（金额 0 但留一条记录）。"""
    provider = _ScriptedLlm(script=[_response('{"preferences": ["photo"]}')])
    planner, trace, ledger, store = _planner(provider)

    first = await _refine(planner)
    assert provider.calls == 1
    assert store.hits, "第一次成功后应已写入缓存"

    # 第二次：模型若被调用就会抛错（说明缓存没接上）
    provider.script = [ProviderError("deepseek", "complete", message="不该被调到")]
    calls_before = provider.calls
    second = await _refine(planner)

    assert provider.calls == calls_before, "命中缓存却仍然调用了模型"
    assert second.parse_source == "hybrid"
    assert first.parse_source == second.parse_source
    assert trace.cache_hits == 1
    cache_entries = [entry for entry in ledger.entries if entry.cache_hit]
    assert cache_entries and cache_entries[-1].amount_cny == Decimal("0")


async def test_narrative_falls_back_when_copy_contains_numbers() -> None:
    provider = _ScriptedLlm(
        script=[
            _response(
                '{"routes": [{"index": 0, "name": "老城散步", "reason": "步行约 2 公里，很轻松"}]}'
            )
        ]
    )
    planner, trace, _, store = _planner(provider)

    narrative = await planner.narrate(_narrative_inputs())

    assert narrative == {}, "含数字的文案必须整条丢弃，退回模板"
    assert trace.tasks[TASK_NARRATIVE] == "rule"
    assert store.writes, "文案本身合法（只是不合格），仍可复用这次的模型输出"


async def test_narrative_happy_path_overrides_templates() -> None:
    provider = _ScriptedLlm(
        script=[
            _response(
                '{"routes": [{"index": 0, "name": "老城散步", "reason": "沿着骑楼慢慢走，适合边逛边吃"}]}'
            )
        ]
    )
    planner, trace, _, _ = _planner(provider)

    narrative = await planner.narrate(_narrative_inputs())

    assert narrative[0].name == "老城散步"
    assert "骑楼" in narrative[0].reason
    assert trace.tasks[TASK_NARRATIVE] == "llm"


async def test_trace_status_reports_honest_facts() -> None:
    _, trace, ledger, _ = _planner(NullLlmProvider(), enabled=False)
    payload = trace.status(
        cost_cny=ledger.spent_cny("llm"), cost_calibrated=True
    ).as_dict()
    assert payload["enabled"] is False
    assert payload["used"] is False
    assert payload["calls"] == 0
    assert payload["cost_cny"] == "0"
    assert payload["prompt_version"] == trace.prompt_version
