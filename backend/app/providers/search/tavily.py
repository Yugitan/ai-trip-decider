"""Tavily 搜索 Provider。

选它是因为免费额度（1000 credits/月）对 MVP 足够，且单价已从官方文档校准
（``config/pricing.yaml`` 的 ``search.tavily``）—— 是当前唯一"成本数字可信"的外部能力。

计费口径（官方文档）：
- Basic Search = 1 credit/request，Advanced Search = 2 credits/request。

★ ``estimate_cost()`` 返回的是 credit **数量**，不是金额 ★
"一次搜索算几个 credit"是本 Provider 的事实（写在这里，因为它由厂商文档定义）；
"一个 credit 值多少人民币"是采购事实（写在 ``config/pricing.yaml`` 的
``search.tavily.credit_price_usd``）。两者分开，才能做到"只改配置就把成本口径改掉"。

实际记账走的是成本层（``PriceBook.search`` 读 pricing.yaml），本方法供上层做
"调之前先估算"。因此这两处的 credit 数必须一致 —— 有测试交叉校验，防止漂移。
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from urllib.parse import urlparse

import httpx

from app.core.errors import ErrorCode, ProviderError
from app.providers.base import ProviderHealth
from app.providers.search.base import Claim, ExtractedFacts, SearchResult, Verification

__all__ = ["TavilyProvider"]

_ENDPOINT = "https://api.tavily.com/search"

# 官方计价单位：basic=1 credit，advanced=2 credits（见 pricing.yaml 的 costs）
_CREDITS_PER_OP = {"search_basic": 1, "search_advanced": 2}


class TavilyProvider:
    name = "tavily"

    def __init__(
        self,
        api_key: str,
        *,
        timeout_s: float = 12.0,
    ) -> None:
        if not api_key:
            raise ValueError("TavilyProvider 需要非空 api_key（无 Key 请使用 SeedOnlyProvider）")
        self._api_key = api_key
        self._timeout_s = timeout_s

    def health(self) -> ProviderHealth:
        return ProviderHealth(name=self.name, available=True, detail="已配置 API Key")

    async def search(
        self,
        query: str,
        *,
        locale: str = "zh-CN",
        max_results: int = 8,
        domains: list[str] | None = None,
        recency_days: int | None = None,
    ) -> list[SearchResult]:
        payload: dict[str, object] = {
            "api_key": self._api_key,
            "query": query,
            "max_results": max_results,
            "search_depth": "basic",
        }
        if domains:
            payload["include_domains"] = domains
        if recency_days is not None:
            # Tavily 只接受固定的天数档位，向上取整到最近的档（宁可宽一点）
            payload["days"] = _nearest_day_bucket(recency_days)

        return _to_results(await self._post(payload))

    async def extract(self, url: str, *, fields: Sequence[str]) -> ExtractedFacts:
        """Tavily 的 extract 需要另一次计费调用；M3 只声明契约、不实现。

        刻意不实现而不是返回空值：返回空值会让上层以为"抽取成功但没抽到"，
        从而把库里的值覆盖成空。抛 ``PROVIDER_UNAVAILABLE`` 更诚实。
        """
        raise ProviderError(
            self.name,
            "extract",
            kind=ErrorCode.PROVIDER_UNAVAILABLE,
            message="Tavily extract 未实现（M3 仅声明契约）；请使用摘要片段抽取",
        )

    async def verify(self, claim: Claim) -> Verification:
        results = await self.search(f"{claim.subject} {claim.field}", max_results=3)
        if not results:
            return Verification(status="unknown", confidence=0.0, note="搜索无结果，无法核验")
        hit = claim.value in " ".join(f"{r.title} {r.snippet}" for r in results)
        if hit:
            return Verification(
                status="confirmed",
                confidence=0.6,
                supporting=tuple(r.url for r in results[:3]),
                note="搜索摘要中出现相同取值（中等可信，非官方来源）",
            )
        return Verification(
            status="conflicting",
            confidence=0.4,
            supporting=tuple(r.url for r in results[:3]),
            note="搜索摘要中未出现相同取值，需人工复核",
        )

    def estimate_cost(self, op: str, units: int = 1) -> Decimal:
        """估算这次调用消耗的 credit 数。

        未知操作名按 basic（1 credit）计：宁可多算也不要少算，
        但**不静默**——调用方应只用 ``search_basic`` / ``search_advanced``。
        """
        return Decimal(_CREDITS_PER_OP.get(op, 1)) * max(0, units)

    async def _post(self, payload: dict[str, object]) -> dict[str, object]:
        try:
            async with httpx.AsyncClient(timeout=self._timeout_s) as client:
                response = await client.post(_ENDPOINT, json=payload)
        except httpx.TimeoutException as exc:
            raise ProviderError(
                self.name, "search", kind=ErrorCode.PROVIDER_TIMEOUT, message="搜索请求超时"
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(
                self.name,
                "search",
                kind=ErrorCode.PROVIDER_UNAVAILABLE,
                message=f"搜索请求失败：{type(exc).__name__}",
            ) from exc

        if response.status_code >= 400:
            raise ProviderError(
                self.name,
                "search",
                kind=ErrorCode.PROVIDER_UNAVAILABLE,
                message=f"搜索返回 HTTP {response.status_code}",
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise ProviderError(
                self.name,
                "search",
                kind=ErrorCode.PROVIDER_INVALID_RESPONSE,
                message="搜索返回了非 JSON 响应",
            ) from exc
        if not isinstance(body, dict):
            raise ProviderError(
                self.name,
                "search",
                kind=ErrorCode.PROVIDER_INVALID_RESPONSE,
                message="搜索返回了非对象结构",
            )
        return body


def _nearest_day_bucket(days: int) -> int:
    """把天数归到 Tavily 支持的档位（1/3/7/14/30）。"""
    for bucket in (1, 3, 7, 14, 30):
        if days <= bucket:
            return bucket
    return 30


def _to_results(body: dict[str, object]) -> list[SearchResult]:
    raw = body.get("results")
    if not isinstance(raw, list):
        return []
    results: list[SearchResult] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url", "")).strip()
        if not url:
            continue
        results.append(
            SearchResult(
                title=str(item.get("title", "")).strip(),
                url=url,
                snippet=str(item.get("content", "")).strip(),
                domain=urlparse(url).netloc or None,
                score=float(item["score"]) if isinstance(item.get("score"), int | float) else None,
            )
        )
    return results
