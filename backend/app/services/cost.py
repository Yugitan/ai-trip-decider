"""成本控制（PRD §15.4、FR-12）。

★ 本文件最重要的一条约定：未校准的单价 ≠ 0 元 ★
``config/pricing.yaml`` 里 DeepSeek/高德/Serper 的单价仍是 ``null``（因为官方价目页
是 JS 渲染，静态抓不到）。把它们当成 0 会让成本报表"看起来"达标，是最危险的
自我欺骗。这里的原则是：

- ``null`` ⇒ 金额记 0，但 ``calibrated=False`` 一路带到 ``cost_logs.pricing_calibrated``，
  报表必须显式标注"含未校准单价，金额不代表真实支出"；
- 因此**熔断不能只依赖金额**。``plan_llm_calls`` / ``plan_map_calls`` /
  搜索查询数都是**按次数**的硬上限，与单价是否校准无关 —— 它们才是真正的刹车。
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import CircuitBreakerLimits, PricingConfig, ProviderLimits
from app.core.errors import CostBreakerOpen
from app.core.logging import get_logger
from app.db.models import CostLog

__all__ = [
    "CostEntry",
    "CostLedger",
    "CostStore",
    "Price",
    "PriceBook",
]

log = get_logger("cost")

BreakerKind = Literal["plan", "search", "map", "llm"]
CostCategory = Literal["search", "llm", "map", "geocode", "tiles", "other"]

_ZERO = Decimal("0")


@dataclass(frozen=True, slots=True)
class Price:
    """一次调用的价格。``calibrated=False`` 表示金额只是量级参考。"""

    amount_cny: Decimal
    calibrated: bool
    unit_price_cny: Decimal = _ZERO


class PriceBook:
    """按 ``config/pricing.yaml`` 计算调用成本。"""

    def __init__(self, pricing: PricingConfig) -> None:
        self._pricing = pricing
        self._fx = Decimal(str(pricing.fx.usd_cny))

    # ── LLM：按 token 计费（美元/百万 token）──

    def llm(
        self,
        provider: str,
        tier: str,
        *,
        tokens_in: int = 0,
        tokens_out: int = 0,
        tokens_cached: int = 0,
    ) -> Price:
        node = _node(self._pricing.llm, provider, tier)
        inputs = _decimal(node.get("input_per_mtok"))
        cached = _decimal(node.get("cached_input_per_mtok"))
        outputs = _decimal(node.get("output_per_mtok"))
        if inputs is None or outputs is None:
            # 缺单价 ⇒ 未知，不是免费。金额记 0 但标记未校准。
            return Price(amount_cny=_ZERO, calibrated=False)

        cached_tokens = min(max(0, tokens_cached), max(0, tokens_in))
        billable_in = max(0, tokens_in - cached_tokens)
        # 缓存命中的 token 若没有单独单价，按普通输入价计（保守，宁可多算）
        cached_rate = cached if cached is not None else inputs
        amount = (
            (Decimal(billable_in) * inputs + Decimal(cached_tokens) * cached_rate + Decimal(max(0, tokens_out)) * outputs)
            / Decimal(1_000_000)
            * self._fx
        )
        unit = (inputs / Decimal(1_000_000) * self._fx).quantize(Decimal("0.00000001"))
        return Price(amount_cny=amount.quantize(Decimal("0.000001")), calibrated=True, unit_price_cny=unit)

    # ── 搜索：按 credit / 千次查询计费 ──

    def search(self, provider: str, operation: str = "search_basic", units: int = 1) -> Price:
        node = self._pricing.search.get(provider)
        if not isinstance(node, Mapping):
            return Price(amount_cny=_ZERO, calibrated=False)
        if bool(node.get("needs_calibration", False)):
            return Price(amount_cny=_ZERO, calibrated=False)
        calls = max(0, units)
        # 免费/固定单价（如 seed_only 的 price_per_query_usd: 0）——已知且为零，不是"未知"
        per_query = _decimal(node.get("price_per_query_usd"))
        if "price_per_query_usd" in node:
            if per_query is None:
                return Price(amount_cny=_ZERO, calibrated=False)
            return Price(
                amount_cny=(Decimal(calls) * per_query * self._fx).quantize(Decimal("0.000001")),
                calibrated=True,
                unit_price_cny=(per_query * self._fx).quantize(Decimal("0.00000001")),
            )
        # Tavily：credit 单价 × 每次查询的 credit 数
        if "credit_price_usd" in node:
            credit_price = _decimal(node.get("credit_price_usd"))
            costs = node.get("costs")
            credits = _decimal(costs.get(operation)) if isinstance(costs, Mapping) else None
            if credit_price is None or credits is None:
                return Price(amount_cny=_ZERO, calibrated=False)
            amount = Decimal(calls) * credits * credit_price * self._fx
            unit = (credits * credit_price * self._fx).quantize(Decimal("0.00000001"))
            return Price(amount_cny=amount.quantize(Decimal("0.000001")), calibrated=True, unit_price_cny=unit)
        # 其余厂商：按每千次查询计价
        per_1k = _decimal(node.get("price_per_1000_queries_usd"))
        if per_1k is None:
            return Price(amount_cny=_ZERO, calibrated=False)
        amount = Decimal(calls) / Decimal(1000) * per_1k * self._fx
        unit = (per_1k / Decimal(1000) * self._fx).quantize(Decimal("0.00000001"))
        return Price(amount_cny=amount.quantize(Decimal("0.000001")), calibrated=True, unit_price_cny=unit)

    # ── 地图：按调用计费（免费服务为 0）──

    def map(self, provider: str, operation: str = "route_per_call", units: int = 1) -> Price:
        node = self._pricing.map.get(provider)
        if not isinstance(node, Mapping):
            return Price(amount_cny=_ZERO, calibrated=False)
        if bool(node.get("needs_calibration", False)):
            return Price(amount_cny=_ZERO, calibrated=False)
        per_call = _decimal(node.get(operation))
        if per_call is None:
            # 该操作没有列价（例如 amap 未取价）→ 未校准，而不是免费
            return Price(amount_cny=_ZERO, calibrated=False)
        amount = Decimal(max(0, units)) * per_call * self._fx
        # unit_price 必须与 amount_cny **同币种**：单价来自配置（美元口径），
        # 不换算就会让 cost_logs.unit_price 与 amount_cny 差一个汇率。
        unit = (per_call * self._fx).quantize(Decimal("0.00000001"))
        return Price(amount_cny=amount.quantize(Decimal("0.000001")), calibrated=True, unit_price_cny=unit)

    # ── 天气：免费或无价 ──

    def weather(self, provider: str) -> Price:
        node = self._pricing.weather.get(provider)
        if not isinstance(node, Mapping) or bool(node.get("needs_calibration", False)):
            return Price(amount_cny=_ZERO, calibrated=False)
        per_call = _decimal(node.get("per_call"))
        if per_call is None:
            return Price(amount_cny=_ZERO, calibrated=False)
        amount = (per_call * self._fx).quantize(Decimal("0.000001"))
        return Price(
            amount_cny=amount,
            calibrated=True,
            unit_price_cny=(per_call * self._fx).quantize(Decimal("0.00000001")),
        )


@dataclass(frozen=True, slots=True)
class CostEntry:
    category: str
    provider: str
    amount_cny: Decimal
    calibrated: bool
    model: str | None = None
    operation: str | None = None
    units: float = 0.0
    unit_price: float = 0.0
    cache_hit: bool = False
    latency_ms: int | None = None


@dataclass
class CostLedger:
    """一次规划的成本账本（进程内）。它同时是**熔断器**。

    ``ensure_allowed(kind)`` 必须在每次外部调用**之前**调用；触发时抛
    ``CostBreakerOpen``，上层据此降级而不是继续花钱。
    """

    price_book: PriceBook
    breaker: CircuitBreakerLimits
    providers: ProviderLimits
    entries: list[CostEntry] = field(default_factory=list)

    # ── 记账 ──

    def record(
        self,
        category: CostCategory | str,
        provider: str,
        *,
        price: Price,
        model: str | None = None,
        operation: str | None = None,
        units: float = 0.0,
        cache_hit: bool = False,
        latency_ms: int | None = None,
    ) -> CostEntry:
        entry = CostEntry(
            category=str(category),
            provider=provider,
            amount_cny=price.amount_cny,
            calibrated=price.calibrated,
            model=model,
            operation=operation,
            units=units,
            unit_price=float(price.unit_price_cny),
            cache_hit=cache_hit,
            latency_ms=latency_ms,
        )
        self.entries.append(entry)
        return entry

    def record_cache_hit(self, category: CostCategory | str, provider: str, *, operation: str | None = None) -> CostEntry:
        """缓存命中也要记账（金额 0），否则"命中率"无法从成本日志算出来。"""
        return self.record(
            category,
            provider,
            price=Price(amount_cny=_ZERO, calibrated=True),
            operation=operation,
            cache_hit=True,
        )

    # ── 汇总 ──

    def spent_cny(self, category: str | None = None) -> Decimal:
        total = _ZERO
        for entry in self.entries:
            if category is None or entry.category == category:
                total += entry.amount_cny
        return total

    def calls(self, category: str) -> int:
        return sum(1 for entry in self.entries if entry.category == category and not entry.cache_hit)

    @property
    def all_calibrated(self) -> bool:
        return all(entry.calibrated for entry in self.entries)

    # ── 熔断 ──

    def ensure_allowed(self, kind: BreakerKind) -> None:
        """在发起外部调用前检查预算。触发则抛 ``CostBreakerOpen``。"""
        if self.spent_cny() > self.breaker.plan_total_cny:
            raise CostBreakerOpen("plan_total", float(self.spent_cny()), self.breaker.plan_total_cny)
        if kind == "search":
            spent = self.spent_cny("search")
            if spent > self.breaker.plan_search_cny:
                raise CostBreakerOpen("search", float(spent), self.breaker.plan_search_cny)
            calls = self.calls("search")
            if calls >= self.providers.search_max_queries_per_plan:
                raise CostBreakerOpen("search_calls", float(calls), float(self.providers.search_max_queries_per_plan))
        elif kind == "map":
            calls = self.calls("map")
            if calls >= self.breaker.plan_map_calls:
                raise CostBreakerOpen("map_calls", float(calls), float(self.breaker.plan_map_calls))
        elif kind == "llm":
            calls = self.calls("llm")
            if calls >= self.breaker.plan_llm_calls:
                raise CostBreakerOpen("llm_calls", float(calls), float(self.breaker.plan_llm_calls))
        elif kind == "plan":
            # plan 只检查总成本（上面已做），这里显式保留分支以便调用方表达意图
            return

    def summary(self) -> dict[str, Any]:
        return {
            "total_cny": str(self.spent_cny()),
            "by_category": {
                category: str(self.spent_cny(category))
                for category in ("llm", "search", "map", "other")
            },
            "calls": {
                category: self.calls(category) for category in ("llm", "search", "map")
            },
            "cache_hits": sum(1 for e in self.entries if e.cache_hit),
            "pricing_calibrated": self.all_calibrated,
        }


@dataclass
class CostStore:
    """``cost_logs`` 的写入与聚合（供 ``/admin/cost`` 与 ``make cost`` 使用）。"""

    session: AsyncSession

    async def persist(
        self,
        entries: Sequence[CostEntry],
        *,
        request_id: uuid.UUID | None = None,
        trip_id: uuid.UUID | None = None,
    ) -> int:
        if not entries:
            return 0
        for entry in entries:
            self.session.add(
                CostLog(
                    request_id=request_id,
                    trip_id=trip_id,
                    category=entry.category,
                    provider=entry.provider,
                    model=entry.model,
                    operation=entry.operation,
                    units=entry.units,
                    unit_price=entry.unit_price,
                    amount_cny=entry.amount_cny,
                    cache_hit=entry.cache_hit,
                    latency_ms=entry.latency_ms,
                    pricing_calibrated=entry.calibrated,
                )
            )
        await self.session.flush()
        return len(entries)

    async def summary(self, *, days: int = 7) -> dict[str, Any]:
        """近 N 天的成本汇总。**未校准的金额会被显式标注**，不做静默美化。"""
        since = datetime.now(UTC) - timedelta(days=days)
        rows = (
            await self.session.execute(
                select(
                    CostLog.category,
                    func.count().label("calls"),
                    func.coalesce(func.sum(CostLog.amount_cny), 0).label("amount"),
                    func.count().filter(CostLog.cache_hit.is_(True)).label("cache_hits"),
                )
                .where(CostLog.created_at >= since)
                .group_by(CostLog.category)
            )
        ).all()
        # 未校准计数必须与上面的分组**用同一个时间窗**：否则表头写"近 7 天"、
        # 数字却是全量历史，两者会互相矛盾，而报表的全部价值就在于这两个数能被相信。
        uncalibrated = (
            await self.session.execute(
                select(func.count())
                .select_from(CostLog)
                .where(CostLog.created_at >= since, CostLog.pricing_calibrated.is_(False))
            )
        ).scalar_one()
        return {
            "days": days,
            "by_category": [
                {
                    "category": row.category,
                    "calls": int(row.calls),
                    "amount_cny": str(row.amount),
                    "cache_hits": int(row.cache_hits),
                }
                for row in rows
            ],
            "uncalibrated_rows": int(uncalibrated),
            "pricing_calibrated": int(uncalibrated) == 0,
            "note": (
                "存在未校准单价，金额不代表真实支出"
                if int(uncalibrated) > 0
                else "全部单价已校准"
            ),
        }

    async def by_provider(self, *, days: int = 7) -> list[dict[str, Any]]:
        """按 provider × category 的成本明细，用于定位"钱花在哪"。"""
        since = datetime.now(UTC) - timedelta(days=days)
        rows = (
            await self.session.execute(
                select(
                    CostLog.provider,
                    CostLog.category,
                    func.count().label("calls"),
                    func.coalesce(func.sum(CostLog.amount_cny), 0).label("amount"),
                    func.count().filter(CostLog.cache_hit.is_(True)).label("cache_hits"),
                )
                .where(CostLog.created_at >= since)
                .group_by(CostLog.provider, CostLog.category)
                .order_by(func.coalesce(func.sum(CostLog.amount_cny), 0).desc())
            )
        ).all()
        return [
            {
                "provider": row.provider,
                "category": row.category,
                "calls": int(row.calls),
                "amount_cny": str(row.amount),
                "cache_hits": int(row.cache_hits),
            }
            for row in rows
        ]


def _node(tree: Mapping[str, Any], *path: str) -> Mapping[str, Any]:
    current: Any = tree
    for key in path:
        if not isinstance(current, Mapping):
            return {}
        current = current.get(key)
    return current if isinstance(current, Mapping) else {}


def _decimal(value: Any) -> Decimal | None:
    """把 YAML 里的数字转成 Decimal；``None`` / 非数字保持 ``None``（= 未知）。"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float | str):
        try:
            return Decimal(str(value))
        except Exception:
            return None
    return None
