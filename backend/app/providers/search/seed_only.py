"""搜索降级实现：``SeedOnlyProvider``。

无搜索 Key 时的默认选择。它**不联网**，``search()`` 返回空列表——
这比"返回假结果"诚实得多：上层据此显示"本次未联网，信息基于本地知识库"
（PRD §15.5 降级矩阵），而不是假装搜到了东西。

``extract()`` 抛 ``PROVIDER_UNAVAILABLE``：没有网络就没有网页可抽取，
调用方必须回退到"标记 unknown"，绝不能编造字段值。
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from app.core.errors import ErrorCode, ProviderError
from app.providers.base import ProviderHealth
from app.providers.search.base import Claim, ExtractedFacts, SearchResult, Verification

__all__ = ["SeedOnlyProvider"]

_REASON = "未配置搜索 API Key（只读本地知识库，不联网）"


class SeedOnlyProvider:
    name = "seed_only"

    def health(self) -> ProviderHealth:
        return ProviderHealth(name=self.name, available=False, detail=_REASON, degraded=True)

    async def search(
        self,
        query: str,
        *,
        locale: str = "zh-CN",
        max_results: int = 8,
        domains: list[str] | None = None,
        recency_days: int | None = None,
    ) -> list[SearchResult]:
        """不联网 ⇒ 没有外部结果。返回空列表是**如实**，不是失败。"""
        return []

    async def extract(self, url: str, *, fields: Sequence[str]) -> ExtractedFacts:
        raise ProviderError(
            self.name,
            "extract",
            kind=ErrorCode.PROVIDER_UNAVAILABLE,
            message=_REASON,
        )

    async def verify(self, claim: Claim) -> Verification:
        return Verification(status="unknown", confidence=0.0, note=_REASON)

    def estimate_cost(self, op: str, units: int = 1) -> Decimal:
        return Decimal("0")
