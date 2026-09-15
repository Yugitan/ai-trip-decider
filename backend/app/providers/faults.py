"""故障 Provider 包装器（配合 ``app/core/faults.py`` 使用）。

只做一件事：把真实 Provider 包一层，在调用点按 ``FAULT_INJECTION`` 抛错或返回坏数据。
**被包装的 Provider 本身照常存在**（它的 ``name`` / ``health()`` 仍然是真的），
因此 /health、成本账本、降级清单看到的都还是那家厂商的名字，
唯一多出来的信息是 ``health().degraded=True`` 且 detail 写明"注入中"。

为什么包在 Provider 外面而不是改 Provider 内部：
Provider 的职责是"跟供应商说话"，注入是测试关注点。混进去以后，
线上代码里就永远留着一条"如果环境变量是 X 就撒谎"的分支 ——
而它最该待的地方是装配工厂（``registry.build_providers``）。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from app.core.errors import ErrorCode, ProviderError
from app.domain.models import TransportMode
from app.providers.base import LatLng, ProviderHealth
from app.providers.llm.base import LlmMessage, LlmProvider, LlmResponse, LlmTier, LlmUsage
from app.providers.map.base import MapLeg, MapProvider
from app.providers.search.base import Claim, ExtractedFacts, SearchProvider, SearchResult, Verification

#: 包装器刻意**不是** frozen：Provider 契约里 ``name: str`` 是可写属性，
#: 而 frozen dataclass 的属性在类型层面是只读的 —— 想两全就得让协议与实现打架。
#: 它们本来就是"临时套一层"的一次性对象，可变不会带来风险。
__all__ = [
    "LLM_FAULTS",
    "MAP_FAULTS",
    "SEARCH_FAULTS",
    "FaultyLlmProvider",
    "FaultyMapProvider",
    "FaultySearchProvider",
]

LLM_FAULTS: frozenset[str] = frozenset({"llm_timeout", "llm_invalid_json", "llm_500"})
SEARCH_FAULTS: frozenset[str] = frozenset({"search_500", "search_empty", "search_timeout"})
MAP_FAULTS: frozenset[str] = frozenset({"map_500", "map_timeout"})

#: 注入返回的"非 JSON"载荷。刻意不是空串：空串可能被当成"模型没说话"而走另一条
#: 分支，我们要测的是 ``complete_json`` 的解析失败 → 重试 → 抛 PROVIDER_INVALID_RESPONSE。
_INVALID_JSON_TEXT = "抱歉，这不是 JSON。"


def _provider_error(provider: str, operation: str, fault: str) -> ProviderError:
    """把故障名翻译成对应的 ProviderError（超时与不可用是两种错误码，都要能被区分）。"""
    if fault.endswith("_timeout"):
        return ProviderError(
            provider,
            operation,
            kind=ErrorCode.PROVIDER_TIMEOUT,
            message=f"注入故障 {fault}：{provider} 的 {operation} 超时",
        )
    return ProviderError(
        provider,
        operation,
        kind=ErrorCode.PROVIDER_UNAVAILABLE,
        message=f"注入故障 {fault}：{provider} 的 {operation} 不可用",
    )


def _fault_health(base: ProviderHealth, fault: str) -> ProviderHealth:
    """注入中的 Provider **必须**被报成降级，否则 /health 会撒谎。

    ``available`` 沿用被包装 Provider 的真实结论：注入不会让一个没配 Key 的
    降级实现突然"可用"，也不会让一个真实现突然"不可用"（它只是这次会被我们拦下）。
    """
    detail = f"{base.detail} · " if base.detail else ""
    return ProviderHealth(
        name=base.name,
        available=base.available,
        degraded=True,
        detail=f"{detail}fault_injection:{fault}",
    )


@dataclass(slots=True)
class FaultyLlmProvider:
    provider: LlmProvider
    fault: str
    #: 与真实 Provider 同名。/health、成本账本、降级清单都靠它认人，
    #: 所以刻意不写成 ``f"{name}+fault"``：注入只是临时把调用拦下来，
    #: 它没有换一家供应商，账也不该记到别人头上。
    name: str = ""

    def __post_init__(self) -> None:
        if not self.name:
            object.__setattr__(self, "name", self.provider.name)

    def model_for(self, tier: LlmTier) -> str:
        return self.provider.model_for(tier)

    async def complete(
        self,
        messages: Sequence[LlmMessage],
        *,
        tier: LlmTier,
        max_output_tokens: int,
        temperature: float = 0.2,
        json_mode: bool = False,
    ) -> LlmResponse:
        if self.fault == "llm_invalid_json":
            # 结构对、内容坏：这是最容易把"模型抽风"变成"用户看到乱码"的那条路径
            return LlmResponse(
                text=_INVALID_JSON_TEXT,
                model=self.provider.model_for(tier),
                provider=self.name,
                usage=LlmUsage(tokens_in=len(_INVALID_JSON_TEXT), tokens_out=len(_INVALID_JSON_TEXT)),
            )
        raise _provider_error(self.name, "complete", self.fault)

    def health(self) -> ProviderHealth:
        return _fault_health(self.provider.health(), self.fault)


@dataclass(slots=True)
class FaultySearchProvider:
    provider: SearchProvider
    fault: str
    name: str = ""

    def __post_init__(self) -> None:
        if not self.name:
            object.__setattr__(self, "name", self.provider.name)

    async def search(
        self,
        query: str,
        *,
        locale: str = "zh-CN",
        max_results: int = 8,
        domains: list[str] | None = None,
        recency_days: int | None = None,
    ) -> list[SearchResult]:
        if self.fault == "search_empty":
            # 空结果**不是错误**：上层要把它当"这层查过了但没查到"，
            # 而不是当成失败去重试或伪装成有新数据。
            return []
        raise _provider_error(self.name, "search", self.fault)

    async def extract(self, url: str, *, fields: Sequence[str]) -> ExtractedFacts:
        return await self.provider.extract(url, fields=fields)

    async def verify(self, claim: Claim) -> Verification:
        return await self.provider.verify(claim)

    def health(self) -> ProviderHealth:
        return _fault_health(self.provider.health(), self.fault)

    def estimate_cost(self, op: str, units: int = 1) -> Decimal:
        return self.provider.estimate_cost(op, units)


@dataclass(slots=True)
class FaultyMapProvider:
    provider: MapProvider
    fault: str
    name: str = ""

    def __post_init__(self) -> None:
        if not self.name:
            object.__setattr__(self, "name", self.provider.name)

    async def leg(
        self, origin: LatLng, destination: LatLng, *, mode: TransportMode
    ) -> MapLeg | None:
        # 无论什么模式都失败：两种故障在这里没有区别（区分它们的是错误码）
        del origin, destination, mode
        raise _provider_error(self.name, "leg", self.fault)

    def health(self) -> ProviderHealth:
        return _fault_health(self.provider.health(), self.fault)

    def estimate_cost(self, op: str, units: int = 1) -> Decimal:
        return self.provider.estimate_cost(op, units)
