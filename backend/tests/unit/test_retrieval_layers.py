"""L5–L8 检索层单元测试。

这些层是"引擎与外部世界之间"的薄适配器，最容易出的问题是**参数没传对**、
**成本没记**、**熔断没查**。用一个可控的替身逐条钉住。
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Any, cast

import pytest

from app.core.config import (
    CircuitBreakerLimits,
    PricingConfig,
    get_limits_config,
    get_pricing_config,
)
from app.core.errors import CostBreakerOpen
from app.providers.llm.base import LlmMessage, LlmResponse, LlmTier, LlmUsage
from app.providers.map.base import MapLeg
from app.providers.search.base import Claim, ExtractedFacts, SearchResult, Verification
from app.services.cache import CacheStore, MemoryCache
from app.services.cost import CostLedger, PriceBook
from app.services.retrieval_chain import RetrievalQuery
from app.services.retrieval_layers import (
    KIND_LEG,
    KIND_LLM,
    KIND_SEARCH,
    CacheLayer,
    LlmLayer,
    MapLayer,
    SearchLayer,
)

pytestmark = pytest.mark.unit


# ── 替身 ────────────────────────────────────────────────────────────────────


class _FakeStore:
    def __init__(self, *, llm: dict[str, Any] | None = None, search: dict[str, Any] | None = None) -> None:
        self.llm = llm
        self.search = search
        self.llm_reads: list[str] = []
        self.search_reads: list[str] = []

    async def get_llm(self, key: str) -> dict[str, Any] | None:
        self.llm_reads.append(key)
        return self.llm

    async def get_search(self, key: str) -> dict[str, Any] | None:
        self.search_reads.append(key)
        return self.search


class _FakeMapProvider:
    name = "fake-map"

    def __init__(self, leg: MapLeg | None) -> None:
        self.leg_value = leg
        self.calls: list[tuple[Any, Any, str]] = []

    async def leg(self, origin: Any, destination: Any, *, mode: str) -> MapLeg | None:
        self.calls.append((origin, destination, mode))
        return self.leg_value


class _FakeLlmProvider:
    def __init__(self, usage: LlmUsage | None = None, *, name: str = "fake-llm") -> None:
        self.calls: list[dict[str, Any]] = []
        self.name = name
        self._usage = usage or LlmUsage(tokens_in=10, tokens_out=5)

    def model_for(self, tier: LlmTier) -> str:
        return f"fake-{tier}"

    async def complete(
        self,
        messages: Sequence[LlmMessage],
        *,
        tier: LlmTier,
        max_output_tokens: int,
        temperature: float = 0.2,
        json_mode: bool = False,
    ) -> LlmResponse:
        self.calls.append(
            {"messages": list(messages), "tier": tier, "json_mode": json_mode, "temperature": temperature}
        )
        return LlmResponse(
            text="ok",
            model="fake",
            provider=self.name,
            usage=self._usage,
        )


class _FakeSearchProvider:
    name = "tavily"

    def __init__(self, results: list[SearchResult]) -> None:
        self.results = results
        self.kwargs: list[dict[str, Any]] = []

    async def search(self, query: str, **kwargs: Any) -> list[SearchResult]:
        self.kwargs.append({"query": query, **kwargs})
        return self.results

    async def extract(self, url: str, *, fields: Sequence[str]) -> ExtractedFacts:
        raise NotImplementedError

    async def verify(self, claim: Claim) -> Verification:
        raise NotImplementedError


#: 一份**单价已校准**的合成定价：真实 ``pricing.yaml`` 里 DeepSeek 单价是 ``null``，
#: 用它做测试会让"未校准"结论来自价格簿而不是被测的代码 —— 典型假阴性。
_CALIBRATED_PRICING = {
    "version": "test",
    "calibrated_at": "2026-01-01",
    "currency": "CNY",
    "sources": {},
    "fx": {"usd_cny": 7.2, "fx_updated_at": "2026-01-01"},
    "llm": {
        "deepseek": {
            "fast": {"input_per_mtok": 0.14, "output_per_mtok": 0.28},
            "strong": {"input_per_mtok": 0.55, "output_per_mtok": 2.19},
        }
    },
    "search": {},
    "map": {},
    "weather": {},
    "budgets": {},
}


def _ledger(*, calibrated_prices: bool = False, **breaker_overrides: Any) -> CostLedger:
    limits = get_limits_config()
    breaker = CircuitBreakerLimits(
        plan_total_cny=breaker_overrides.get("plan_total_cny", 999.0),
        plan_search_cny=breaker_overrides.get("plan_search_cny", 999.0),
        plan_map_calls=breaker_overrides.get("plan_map_calls", 99),
        plan_llm_calls=breaker_overrides.get("plan_llm_calls", 99),
    )
    pricing = (
        PricingConfig.model_validate(_CALIBRATED_PRICING) if calibrated_prices else get_pricing_config()
    )
    return CostLedger(price_book=PriceBook(pricing), breaker=breaker, providers=limits.providers)


# ── L5 缓存层 ───────────────────────────────────────────────────────────────


async def test_cache_layer_memory_hit_avoids_database() -> None:
    memory: MemoryCache[dict[str, Any]] = MemoryCache()
    memory.set("k", {"v": 1})
    store = _FakeStore()
    layer = CacheLayer(store=cast(CacheStore, store), memory=memory)
    result = await layer.resolve(RetrievalQuery(kind=KIND_LLM, key="k", payload={"cache_key": "k"}))
    assert result is not None
    assert result.cached is True
    assert result.resolved_by == "L5"
    assert store.llm_reads == [], "内存命中就不该再查数据库"


async def test_cache_layer_promotes_database_hit_into_memory() -> None:
    memory: MemoryCache[dict[str, Any]] = MemoryCache()
    store = _FakeStore(llm={"answer": 42})
    layer = CacheLayer(store=cast(CacheStore, store), memory=memory)
    result = await layer.resolve(RetrievalQuery(kind=KIND_LLM, key="k", payload={"cache_key": "k"}))
    assert result is not None
    assert result.value == {"answer": 42}
    assert memory.get("k") == {"answer": 42}


async def test_cache_layer_reads_search_table_for_search_kind() -> None:
    store = _FakeStore(search={"results": []})
    layer = CacheLayer(store=cast(CacheStore, store))
    await layer.resolve(RetrievalQuery(kind=KIND_SEARCH, key="k", payload={"cache_key": "k"}))
    assert store.search_reads == ["k"]


async def test_cache_layer_returns_none_when_store_misses() -> None:
    store = _FakeStore(llm=None)
    layer = CacheLayer(store=cast(CacheStore, store))
    result = await layer.resolve(RetrievalQuery(kind=KIND_LLM, key="k", payload={"cache_key": "k"}))
    assert result is None


async def test_cache_layer_without_key_or_store_returns_none() -> None:
    layer = CacheLayer(store=None, memory=MemoryCache())
    assert await layer.resolve(RetrievalQuery(kind=KIND_LLM, key="k", payload={})) is None
    assert await layer.resolve(RetrievalQuery(kind=KIND_LLM, key="k", payload={"cache_key": "k"})) is None


# ── L6 地图层 ───────────────────────────────────────────────────────────────


async def test_map_layer_calls_provider_and_records_cost() -> None:
    leg = MapLeg(distance_m=1000, duration_min=None, distance_source="osrm", mode="walk")
    provider = _FakeMapProvider(leg)
    ledger = _ledger()
    layer = MapLayer(provider=cast(Any, provider), ledger=ledger)
    result = await layer.resolve(
        RetrievalQuery(
            kind=KIND_LEG,
            key="p1:p2",
            payload={
                "origin": {"lat": 23.1, "lng": 113.2},
                "destination": {"lat": 23.2, "lng": 113.3},
                "mode": "walk",
            },
        )
    )
    assert result is not None
    assert result.value is leg
    assert provider.calls[0][2] == "walk"
    assert [e.category for e in ledger.entries] == ["map"]


async def test_map_layer_returns_none_for_incomplete_payload() -> None:
    layer = MapLayer(provider=cast(Any, _FakeMapProvider(None)))
    assert await layer.resolve(RetrievalQuery(kind=KIND_LEG, key="k", payload={})) is None
    assert (
        await layer.resolve(
            RetrievalQuery(
                kind=KIND_LEG,
                key="k",
                payload={"origin": {"lat": "x", "lng": 1.0}, "destination": {"lat": 1.0, "lng": 1.0}},
            )
        )
        is None
    )


async def test_map_layer_returns_none_when_provider_does_not_support_mode() -> None:
    provider = _FakeMapProvider(None)
    layer = MapLayer(provider=cast(Any, provider))
    payload = {
        "origin": {"lat": 23.1, "lng": 113.2},
        "destination": {"lat": 23.2, "lng": 113.3},
        "mode": "ferry",
    }
    assert await layer.resolve(RetrievalQuery(kind=KIND_LEG, key="k", payload=payload)) is None


async def test_map_layer_respects_cost_breaker() -> None:
    provider = _FakeMapProvider(MapLeg(distance_m=1, duration_min=1, distance_source="amap"))
    layer = MapLayer(provider=cast(Any, provider), ledger=_ledger(plan_map_calls=0))
    with pytest.raises(CostBreakerOpen):
        await layer.resolve(
            RetrievalQuery(
                kind=KIND_LEG,
                key="k",
                payload={"origin": {"lat": 1.0, "lng": 1.0}, "destination": {"lat": 2.0, "lng": 2.0}},
            )
        )


# ── L7 LLM 层 ───────────────────────────────────────────────────────────────


async def test_llm_layer_parses_messages_and_records_tokens() -> None:
    provider = _FakeLlmProvider()
    ledger = _ledger()
    layer = LlmLayer(provider=cast(Any, provider), ledger=ledger)
    result = await layer.resolve(
        RetrievalQuery(
            kind=KIND_LLM,
            key="k",
            payload={
                "messages": [
                    {"role": "system", "content": "s"},
                    {"role": "user", "content": "u"},
                    {"role": "weird", "content": "x"},
                ],
                "tier": "strong",
                "max_output_tokens": 64,
                "json_mode": True,
                "task": "rerank",
            },
        )
    )
    assert result is not None
    call = provider.calls[0]
    assert call["tier"] == "strong"
    assert call["json_mode"] is True
    assert [m.role for m in call["messages"]] == ["system", "user", "user"], "未知 role 必须退回 user"
    entry = ledger.entries[0]
    assert entry.category == "llm"
    assert entry.units == 15.0


async def test_llm_layer_returns_none_without_messages() -> None:
    layer = LlmLayer(provider=cast(Any, _FakeLlmProvider()))
    assert await layer.resolve(RetrievalQuery(kind=KIND_LLM, key="k", payload={"messages": []})) is None
    assert await layer.resolve(RetrievalQuery(kind=KIND_LLM, key="k", payload={})) is None


async def test_llm_layer_treats_string_messages_as_no_payload() -> None:
    """``str`` 也是 ``Sequence``，放它过去会把``"你好"``拆成两条单字消息（静默地）。"""
    provider = _FakeLlmProvider()
    layer = LlmLayer(provider=cast(Any, provider))
    for bogus in ("你好", b"hi", 42, None, {"role": "user"}):
        assert (
            await layer.resolve(RetrievalQuery(kind=KIND_LLM, key="k", payload={"messages": bogus}))
        ) is None
    assert provider.calls == [], "形状错的载荷根本不该发出去"


async def test_llm_layer_tolerates_bad_numeric_options() -> None:
    """``int(None)`` 会抛 TypeError，而检索链只捕 ProviderError —— 那会变成 500。"""
    provider = _FakeLlmProvider()
    layer = LlmLayer(provider=cast(Any, provider))
    await layer.resolve(
        RetrievalQuery(
            kind=KIND_LLM,
            key="k",
            payload={
                "messages": [{"role": "user", "content": "x"}],
                "max_output_tokens": None,
                "temperature": "not-a-number",
            },
        )
    )
    assert provider.calls[0]["temperature"] == 0.2


async def test_llm_layer_marks_cost_uncalibrated_when_usage_unknown() -> None:
    """厂商没返回 token 数时，**不能记成"已知的 0 元"**。

    这与 ``pricing.yaml`` 里单价为 ``null`` 是同一件事：未知不是免费。
    把 0 token 当场已知，等于在成本报表里向用户声称"这次调用没花钱"。
    """
    unknown = _FakeLlmProvider(usage=LlmUsage(usage_known=False), name="deepseek")
    ledger = _ledger(calibrated_prices=True)
    layer = LlmLayer(provider=cast(Any, unknown), ledger=ledger)
    query = RetrievalQuery(kind=KIND_LLM, key="k", payload={"messages": [{"role": "user", "content": "x"}]})
    await layer.resolve(query)
    entry = ledger.entries[0]
    assert entry.amount_cny == Decimal("0")
    assert entry.calibrated is False, "用量未知 ⇒ 成本不可信，必须如实标注"
    assert ledger.all_calibrated is False

    # 对照组：同一份合成定价下，厂商**有**返回 token 数时成本是可信的（即使这次恰好 0）。
    # 两个用例一起才能证明 `calibrated` 是被 `usage_known` 决定的，而不是恒为 False。
    zero_but_known = _FakeLlmProvider(usage=LlmUsage(tokens_in=0, tokens_out=0), name="deepseek")
    known_ledger = _ledger(calibrated_prices=True)
    await LlmLayer(provider=cast(Any, zero_but_known), ledger=known_ledger).resolve(query)
    assert known_ledger.entries[0].calibrated is True


async def test_llm_layer_accepts_plain_string_messages() -> None:
    provider = _FakeLlmProvider()
    layer = LlmLayer(provider=cast(Any, provider))
    await layer.resolve(
        RetrievalQuery(kind=KIND_LLM, key="k", payload={"messages": ["hello"]})
    )
    assert provider.calls[0]["messages"][0].content == "hello"
    assert provider.calls[0]["messages"][0].role == "user"


async def test_llm_layer_accepts_prebuilt_messages() -> None:
    provider = _FakeLlmProvider()
    layer = LlmLayer(provider=cast(Any, provider))
    result = await layer.resolve(
        RetrievalQuery(kind=KIND_LLM, key="k", payload={"messages": [LlmMessage(role="user", content="hi")]})
    )
    assert result is not None
    assert provider.calls[0]["tier"] == "fast"
    assert provider.calls[0]["json_mode"] is False


async def test_llm_layer_respects_cost_breaker() -> None:
    layer = LlmLayer(provider=cast(Any, _FakeLlmProvider()), ledger=_ledger(plan_llm_calls=0))
    with pytest.raises(CostBreakerOpen):
        await layer.resolve(
            RetrievalQuery(kind=KIND_LLM, key="k", payload={"messages": [{"role": "user", "content": "x"}]})
        )


# ── L8 搜索层 ───────────────────────────────────────────────────────────────


async def test_search_layer_passes_options_and_records_cost() -> None:
    results = [SearchResult(title="t", url="https://example.com", snippet="s")]
    provider = _FakeSearchProvider(results)
    ledger = _ledger()
    layer = SearchLayer(provider=cast(Any, provider), ledger=ledger)
    outcome = await layer.resolve(
        RetrievalQuery(
            kind=KIND_SEARCH,
            key="q",
            payload={
                "query": "广州 美食",
                "max_results": 3,
                "domains": ["gz.gov.cn"],
                "recency_days": 7,
            },
        )
    )
    assert outcome is not None
    assert outcome.value == results
    assert provider.kwargs[0]["max_results"] == 3
    assert provider.kwargs[0]["domains"] == ["gz.gov.cn"]
    assert provider.kwargs[0]["recency_days"] == 7
    entry = ledger.entries[0]
    assert entry.category == "search"
    assert entry.calibrated is True, "tavily 单价已校准，成本必须可信"
    assert entry.amount_cny > Decimal("0")


async def test_search_layer_returns_empty_result_as_a_hit() -> None:
    """空结果也是"这层用过了"：链的末尾必须终止，而不是继续找不存在的第九层。"""
    provider = _FakeSearchProvider([])
    layer = SearchLayer(provider=cast(Any, provider))
    outcome = await layer.resolve(RetrievalQuery(kind=KIND_SEARCH, key="q", payload={"query": "x"}))
    assert outcome is not None
    assert outcome.value == []


async def test_search_layer_does_not_split_string_domains_into_characters() -> None:
    """``domains="example.com"`` 被逐字符迭代会变成 11 个域名约束，而且不报错。"""
    provider = _FakeSearchProvider([])
    layer = SearchLayer(provider=cast(Any, provider))
    await layer.resolve(
        RetrievalQuery(kind=KIND_SEARCH, key="q", payload={"query": "x", "domains": "example.com"})
    )
    assert provider.kwargs[0]["domains"] is None


async def test_search_layer_tolerates_bad_option_types() -> None:
    provider = _FakeSearchProvider([])
    layer = SearchLayer(provider=cast(Any, provider))
    await layer.resolve(
        RetrievalQuery(
            kind=KIND_SEARCH,
            key="q",
            payload={"query": "x", "max_results": None, "recency_days": "七天"},
        )
    )
    assert provider.kwargs[0]["max_results"] == 8
    assert provider.kwargs[0]["recency_days"] is None

    # 非数字字符串会走 ``int("很多")`` → ValueError 这条路（不护着就是 500）
    await layer.resolve(
        RetrievalQuery(kind=KIND_SEARCH, key="q", payload={"query": "x", "max_results": "很多"})
    )
    assert provider.kwargs[1]["max_results"] == 8


async def test_search_layer_ignores_blank_query() -> None:
    provider = _FakeSearchProvider([])
    layer = SearchLayer(provider=cast(Any, provider))
    assert await layer.resolve(RetrievalQuery(kind=KIND_SEARCH, key="q", payload={"query": "  "})) is None
    assert provider.kwargs == []


async def test_search_layer_respects_cost_breaker() -> None:
    from app.services.cost import Price

    provider = _FakeSearchProvider([])
    ledger = _ledger(plan_search_cny=0.01)
    ledger.record("search", "tavily", price=Price(amount_cny=Decimal("0.05"), calibrated=True))
    layer = SearchLayer(provider=cast(Any, provider), ledger=ledger)
    with pytest.raises(CostBreakerOpen):
        await layer.resolve(RetrievalQuery(kind=KIND_SEARCH, key="q", payload={"query": "x"}))

