"""搜索 Provider 契约（PRD §13.1）。

★ 关于隐私与存储 ★
``SearchResult`` 只保留**标题 / URL / 摘要片段**，不存网页全文（PRD §13.2 禁止项）。
摘要片段用于事实抽取，抽取结果进 ``travel_sources.extracted_facts``，
原文只留 ``content_hash``。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, Protocol, runtime_checkable

from app.providers.base import ProviderHealth

__all__ = [
    "Claim",
    "ExtractedFacts",
    "SearchProvider",
    "SearchResult",
    "Verification",
    "VerificationStatus",
]

VerificationStatus = Literal["confirmed", "conflicting", "unknown"]


@dataclass(frozen=True, slots=True)
class SearchResult:
    title: str
    url: str
    snippet: str = ""
    domain: str | None = None
    score: float | None = None
    published_at: str | None = None


@dataclass(frozen=True, slots=True)
class ExtractedFacts:
    """从一个页面抽取出的字段级事实。``fields`` 的键是字段名（如 opening_hours）。"""

    url: str
    fields: Mapping[str, str]
    source_name: str = ""
    credibility: float = 0.5


@dataclass(frozen=True, slots=True)
class Claim:
    """一条待核验的断言。``existing`` 是库里已有的值（可能为空）。"""

    subject: str
    field: str
    value: str
    existing: str | None = None


@dataclass(frozen=True, slots=True)
class Verification:
    status: VerificationStatus
    confidence: float = 0.0
    supporting: tuple[str, ...] = ()
    note: str = ""


@runtime_checkable
class SearchProvider(Protocol):
    name: str

    async def search(
        self,
        query: str,
        *,
        locale: str = "zh-CN",
        max_results: int = 8,
        domains: list[str] | None = None,
        recency_days: int | None = None,
    ) -> list[SearchResult]: ...

    async def extract(self, url: str, *, fields: Sequence[str]) -> ExtractedFacts: ...

    async def verify(self, claim: Claim) -> Verification: ...

    def health(self) -> ProviderHealth: ...

    def estimate_cost(self, op: str, units: int = 1) -> Decimal: ...
