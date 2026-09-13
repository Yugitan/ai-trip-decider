"""L5–L8 的具体检索层：缓存、地图、LLM、实时搜索。

L1–L4（城市知识库 / 热门地点 / 路线模板 / 关系图）是数据库查询，
由 M4 的 ``plan_service`` 在装配时注入 —— 它们的数据来源是本地表，
不需要 Provider 抽象，放在这里反而会把编排逻辑混进引擎。

每层都遵守两条纪律：
1. **成功才计成本**，失败/降级由 ``RetrievalChain`` 记录；
2. 调用外部 Provider **之前**先过 ``CostLedger.ensure_allowed()``（成本熔断）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.domain.models import TransportMode
from app.providers.base import LatLng
from app.providers.llm.base import LlmMessage, LlmProvider, LlmTier, Role
from app.providers.map.base import MapProvider
from app.providers.search.base import SearchProvider
from app.services.cache import CacheStore, MemoryCache
from app.services.cost import CostLedger, Price
from app.services.retrieval_chain import RetrievalQuery, RetrievalResult

__all__ = [
    "KIND_LEG",
    "KIND_LLM",
    "KIND_SEARCH",
    "CacheLayer",
    "LlmLayer",
    "MapLayer",
    "SearchLayer",
]

KIND_LEG = "leg"
KIND_LLM = "llm_json"
KIND_SEARCH = "search"


@dataclass
class CacheLayer:
    """L5：搜索缓存 / LLM 缓存。**命中即完全不花钱**，所以排在外部调用之前。"""

    store: CacheStore | None = None
    memory: MemoryCache[dict[str, Any]] | None = None
    id: str = "L5"
    kinds: frozenset[str] = frozenset({KIND_LLM, KIND_SEARCH})

    async def resolve(self, query: RetrievalQuery) -> RetrievalResult | None:
        cache_key = str(query.payload.get("cache_key", ""))
        if not cache_key:
            return None

        if self.memory is not None:
            hot = self.memory.get(cache_key)
            if hot is not None:
                return RetrievalResult(value=hot, resolved_by=self.id, cached=True)

        if self.store is None:
            return None
        value = (
            await self.store.get_llm(cache_key)
            if query.kind == KIND_LLM
            else await self.store.get_search(cache_key)
        )
        if value is None:
            return None
        if self.memory is not None:
            self.memory.set(cache_key, value)
        return RetrievalResult(value=value, resolved_by=self.id, cached=True)


@dataclass
class MapLayer:
    """L6：地图 API。真实距离/耗时（来源随 Provider 而异，可能仍是估计值）。"""

    provider: MapProvider
    ledger: CostLedger | None = None
    id: str = "L6"
    kinds: frozenset[str] = frozenset({KIND_LEG})

    async def resolve(self, query: RetrievalQuery) -> RetrievalResult | None:
        payload = query.payload
        origin = _point(payload.get("origin"))
        destination = _point(payload.get("destination"))
        if origin is None or destination is None:
            return None
        mode = str(payload.get("mode", "walk"))
        transport_mode: TransportMode = mode  # type: ignore[assignment]
        if self.ledger is not None:
            self.ledger.ensure_allowed("map")
        leg = await self.provider.leg(origin, destination, mode=transport_mode)
        if leg is None:
            return None
        if self.ledger is not None:
            price = self.ledger.price_book.map(self.provider.name, units=1)
            self.ledger.record("map", self.provider.name, price=price, operation="route", units=1)
        return RetrievalResult(value=leg, resolved_by=self.id)


@dataclass
class LlmLayer:
    """L7：AI 推理。贵且慢，必须排在缓存与确定性算法之后。"""

    provider: LlmProvider
    ledger: CostLedger | None = None
    id: str = "L7"
    kinds: frozenset[str] = frozenset({KIND_LLM})

    async def resolve(self, query: RetrievalQuery) -> RetrievalResult | None:
        payload = query.payload
        raw_messages = _items(payload.get("messages"))
        if not raw_messages:
            return None
        messages = [_message(item) for item in raw_messages]
        tier: LlmTier = "strong" if payload.get("tier") == "strong" else "fast"
        max_output_tokens = _int_or(payload.get("max_output_tokens"), 1024)
        temperature = _float_or(payload.get("temperature"), 0.2)

        if self.ledger is not None:
            self.ledger.ensure_allowed("llm")
        response = await self.provider.complete(
            messages,
            tier=tier,
            max_output_tokens=max_output_tokens,
            temperature=temperature,
            json_mode=bool(payload.get("json_mode", False)),
        )
        if self.ledger is not None:
            usage = response.usage
            if usage.usage_known:
                price = self.ledger.price_book.llm(
                    self.provider.name,
                    tier,
                    tokens_in=usage.tokens_in,
                    tokens_out=usage.tokens_out,
                    tokens_cached=usage.tokens_cached,
                )
            else:
                # 厂商没返回 token 数 ⇒ 用量未知。金额记 0 但 calibrated=False，
                # 与"单价为 null"同理：**未知不是免费**，报表必须如实标注。
                price = Price(amount_cny=Decimal("0"), calibrated=False)
            self.ledger.record(
                "llm",
                self.provider.name,
                price=price,
                model=response.model,
                operation=str(payload.get("task", "complete")),
                units=float(usage.tokens_in + usage.tokens_out),
            )
        return RetrievalResult(value=response, resolved_by=self.id)


@dataclass
class SearchLayer:
    """L8：实时联网搜索。**例外路径**，只在库内数据不足时到达这里。"""

    provider: SearchProvider
    ledger: CostLedger | None = None
    id: str = "L8"
    kinds: frozenset[str] = frozenset({KIND_SEARCH})

    async def resolve(self, query: RetrievalQuery) -> RetrievalResult | None:
        payload = query.payload
        text = str(payload.get("query", "")).strip()
        if not text:
            return None
        domains = _items(payload.get("domains"))
        recency = payload.get("recency_days")

        if self.ledger is not None:
            self.ledger.ensure_allowed("search")
        results = await self.provider.search(
            text,
            locale=str(payload.get("locale", "zh-CN")),
            max_results=_int_or(payload.get("max_results"), 8),
            domains=[str(d) for d in domains] if domains else None,
            recency_days=_int_or_none(recency),
        )
        if self.ledger is not None:
            # 用 Provider 自己声明的计价单位估算（credit/次数），由 PriceBook 换算成金额
            price = self.ledger.price_book.search(self.provider.name, "search_basic", units=1)
            self.ledger.record("search", self.provider.name, price=price, operation=text, units=1)
        # 空结果也是"这层用过了"：不能再往下（没有第九层），返回空列表让上层如实告知用户
        return RetrievalResult(value=results, resolved_by=self.id)


def _items(value: Any) -> Sequence[Any] | None:
    """只接受**真正的数组**。

    ``str`` / ``bytes`` 也是 ``Sequence``，``isinstance`` 检查放它们过去会把
    ``messages="你好"`` 拆成两条单字消息、把 ``domains="a.com"`` 拆成 5 个域名约束，
    而且**不会报错** —— 只会静默地发出一个荒谬的请求。精确点：``json_mode`` 之外
    的所有载荷都是外部输入，形状错了应当被当作"本层不处理"而不是被强行解释。
    """
    if isinstance(value, str | bytes | bytearray) or not isinstance(value, Sequence):
        return None
    return value


def _int_or(value: Any, default: int) -> int:
    """载荷里的整数，脏值回退到默认值（``int(None)`` 会抛 TypeError 穿过检索链）。"""
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _int_or_none(value: Any) -> int | None:
    """可缺省的整数（``recency_days`` 不传就是"不限时效"，不是 0 天）。"""
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _float_or(value: Any, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _point(value: Any) -> LatLng | None:
    if not isinstance(value, Mapping):
        return None
    lat, lng = value.get("lat"), value.get("lng")
    if not isinstance(lat, int | float) or not isinstance(lng, int | float):
        return None
    return LatLng(lat=float(lat), lng=float(lng))


def _message(item: Any) -> LlmMessage:
    if isinstance(item, LlmMessage):
        return item
    if isinstance(item, Mapping):
        role = str(item.get("role", "user"))
        safe_role: Role = role if role in ("system", "user", "assistant") else "user"  # type: ignore[assignment]
        return LlmMessage(role=safe_role, content=str(item.get("content", "")))
    return LlmMessage(role="user", content=str(item))
