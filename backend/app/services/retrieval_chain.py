"""8 级优先级检索瀑布（PRD §15.1，架构核心）。

> **铁律：任何一层能解决问题，绝不下降到下一层。**

代码结构体现为一条链：每层实现同一个 ``resolve()``，命中即短路返回并记录
``resolved_by`` 层级（用于统计与优化）。当前 M3 落地的是**引擎**与 L5–L8 的
具体实现（缓存/地图/LLM/搜索）；L1–L4 是数据库查询，由 M4 的 ``plan_service``
在装配时注入（引擎不关心数据从哪来）。

★ 为什么单层失败必须继续往下走 ★
外部依赖（LLM/搜索/地图）随时可能挂。如果一层抛错就中断整条链，用户会看到
"规划失败"，而实际上本地知识库完全够用（PRD §15.5 降级矩阵）。
因此引擎捕获 ``ProviderError`` 与 ``CostBreakerOpen`` 后**记一次失败、继续下一层**，
并把 ``degraded=True`` 如实带出去。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal, Protocol, runtime_checkable

from app.core.errors import CostBreakerOpen, ProviderError
from app.core.logging import get_logger

__all__ = [
    "LAYER_SPECS",
    "FunctionLayer",
    "LayerAttempt",
    "LayerSpec",
    "LayerStatus",
    "RetrievalChain",
    "RetrievalLayer",
    "RetrievalOutcome",
    "RetrievalQuery",
    "RetrievalResult",
    "spec_for",
]

log = get_logger("retrieval")

_ZERO = Decimal("0")

LayerStatus = Literal["hit", "miss", "skipped", "error"]


@dataclass(frozen=True, slots=True)
class LayerSpec:
    """一层的静态元数据（与 PRD §15.1 的表格逐行对应）。

    ``hit_rate_target`` 只用于展示与报表，**不参与任何决策** ——
    它不是"必须达到"的指标，而是"这一层本该承担多少命中"的预期。
    """

    id: str
    key: str
    label: str
    latency_ms: int
    cost_cny: float
    hit_rate_target: str


LAYER_SPECS: tuple[LayerSpec, ...] = (
    LayerSpec("L1", "city_knowledge", "城市知识库（Postgres）", 20, 0.0, "~95%"),
    LayerSpec("L2", "popular_places", "热门地点库", 30, 0.0, ""),
    LayerSpec("L3", "route_templates", "热门路线库（模板复用）", 50, 0.0, "~40%（冷启动）"),
    LayerSpec("L4", "place_relations", "地点关系数据（缓存图）", 50, 0.0, ""),
    LayerSpec("L5", "caches", "搜索缓存 / LLM 缓存", 20, 0.0, "~55%（LLM）"),
    LayerSpec("L6", "map_api", "地图 API（距离/路径）", 300, 0.001, "按需"),
    LayerSpec("L7", "ai_inference", "AI 推理（排序/叙事）", 3000, 0.02, "每规划 1–2 次"),
    LayerSpec("L8", "web_search", "实时联网搜索", 3500, 0.03, "仅例外路径"),
)

_SPEC_BY_ID = {spec.id: spec for spec in LAYER_SPECS}


def spec_for(layer_id: str) -> LayerSpec | None:
    return _SPEC_BY_ID.get(layer_id)


@dataclass(frozen=True, slots=True)
class RetrievalQuery:
    """一次"我需要某条信息"的请求。

    ``kind`` 是信息类别（如 ``"leg"`` / ``"llm_json"`` / ``"search"``），
    层通过 :attr:`RetrievalLayer.kinds` 声明自己能处理哪些类别；
    ``payload`` 是层自定义的参数（引擎不解释它）。
    """

    kind: str
    key: str
    payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    value: Any
    resolved_by: str
    cost_cny: Decimal = _ZERO
    cached: bool = False


@dataclass(frozen=True, slots=True)
class LayerAttempt:
    """一层在这次解析中的结果，用于"为什么走了这么远"的可解释性。"""

    layer_id: str
    status: LayerStatus
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class RetrievalOutcome:
    value: Any
    resolved_by: str | None
    attempts: tuple[LayerAttempt, ...] = ()
    degraded: bool = False

    @property
    def resolved(self) -> bool:
        return self.resolved_by is not None

    @property
    def failed_layers(self) -> tuple[str, ...]:
        return tuple(a.layer_id for a in self.attempts if a.status == "error")


@runtime_checkable
class RetrievalLayer(Protocol):
    id: str
    #: 该层能处理的 ``kind``；空集合表示"任何类别都试"。
    kinds: frozenset[str]

    async def resolve(self, query: RetrievalQuery) -> RetrievalResult | None: ...


class FunctionLayer:
    """把一个异步函数适配成一层。用于装配 DB 查询、测试替身与一次性逻辑。"""

    def __init__(
        self,
        layer_id: str,
        fn: Callable[[RetrievalQuery], Awaitable[RetrievalResult | None]],
        *,
        kinds: Iterable[str] = (),
    ) -> None:
        self.id = layer_id
        self.kinds = frozenset(kinds)
        self._fn = fn

    def supports(self, kind: str) -> bool:
        return not self.kinds or kind in self.kinds

    async def resolve(self, query: RetrievalQuery) -> RetrievalResult | None:
        return await self._fn(query)


@dataclass
class RetrievalChain:
    """按顺序尝试各层，命中即短路。"""

    layers: Sequence[RetrievalLayer]

    def __post_init__(self) -> None:
        ids = [layer.id for layer in self.layers]
        duplicates = {layer_id for layer_id in ids if ids.count(layer_id) > 1}
        if duplicates:
            raise ValueError(f"检索链存在重复层级：{sorted(duplicates)}（层级 id 必须唯一，否则无法归因）")

    @property
    def layer_ids(self) -> tuple[str, ...]:
        return tuple(layer.id for layer in self.layers)

    def describe(self) -> list[dict[str, Any]]:
        """供报表/调试展示：每层的静态元数据 + 该类别的查询是否会经过它。"""
        described: list[dict[str, Any]] = []
        for layer in self.layers:
            spec = spec_for(layer.id)
            described.append(
                {
                    "id": layer.id,
                    "key": spec.key if spec else None,
                    "label": spec.label if spec else layer.id,
                    "latency_ms": spec.latency_ms if spec else None,
                    "cost_cny": spec.cost_cny if spec else None,
                }
            )
        return described

    async def resolve(self, query: RetrievalQuery) -> RetrievalOutcome:
        attempts: list[LayerAttempt] = []
        degraded = False
        for layer in self.layers:
            if not _supports(layer, query.kind):
                attempts.append(LayerAttempt(layer.id, "skipped"))
                continue
            try:
                result = await layer.resolve(query)
            except (ProviderError, CostBreakerOpen) as exc:
                # 外部依赖失败/成本熔断：如实记录后继续往下走（降级矩阵）
                degraded = True
                code = str(getattr(exc, "code", type(exc).__name__))
                attempts.append(LayerAttempt(layer.id, "error", code))
                log.warning(
                    "检索层失败，继续下一层",
                    extra={
                        "event": "retrieval.layer_error",
                        "code": code,
                        "context": {"layer": layer.id, "kind": query.kind},
                    },
                )
                continue
            if result is None:
                attempts.append(LayerAttempt(layer.id, "miss"))
                continue
            attempts.append(LayerAttempt(layer.id, "hit"))
            return RetrievalOutcome(
                value=result.value,
                resolved_by=result.resolved_by or layer.id,
                attempts=tuple(attempts),
                degraded=degraded,
            )

        return RetrievalOutcome(value=None, resolved_by=None, attempts=tuple(attempts), degraded=degraded)


def _supports(layer: RetrievalLayer, kind: str) -> bool:
    """层可选地实现 ``supports()``；没实现就按 ``kinds`` 判断。"""
    supports = getattr(layer, "supports", None)
    if callable(supports):
        return bool(supports(kind))
    kinds: frozenset[str] = getattr(layer, "kinds", frozenset())
    return not kinds or kind in kinds
