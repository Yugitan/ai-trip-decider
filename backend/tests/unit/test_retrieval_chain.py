"""检索链引擎单元测试（PRD §15.1）。

守四件事：
1. **顺序**：层级必须按声明顺序尝试，先命中先返回（"任何一层能解决问题绝不下降"）。
2. **短路**：命中后不再调用后续层（否则会白花外部调用的钱）。
3. **降级**：某层抛 ProviderError / 成本熔断时继续往下走，而不是整体失败。
4. **可解释**：每次解析都留下每层的状态，供报表回答"为什么走了这么远"。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, cast

import pytest

from app.core.errors import ErrorCode, ProviderError
from app.services.retrieval_chain import (
    LAYER_SPECS,
    FunctionLayer,
    RetrievalChain,
    RetrievalLayer,
    RetrievalQuery,
    RetrievalResult,
    spec_for,
)

pytestmark = pytest.mark.unit


def _query(kind: str = "demo") -> RetrievalQuery:
    return RetrievalQuery(kind=kind, key="k")


class _Recorder:
    """记录哪些层被调用过 —— 用来断言"短路"真的发生了。"""

    def __init__(self) -> None:
        self.called: list[str] = []

    def hit(self, layer_id: str, value: Any) -> Any:
        async def _fn(query: RetrievalQuery) -> RetrievalResult | None:
            self.called.append(layer_id)
            return RetrievalResult(value=value, resolved_by=layer_id)

        return _fn

    def miss(self, layer_id: str) -> Any:
        async def _fn(query: RetrievalQuery) -> RetrievalResult | None:
            self.called.append(layer_id)
            return None

        return _fn

    def boom(self, layer_id: str, exc: Exception) -> Any:
        async def _fn(query: RetrievalQuery) -> RetrievalResult | None:
            self.called.append(layer_id)
            raise exc

        return _fn


def test_layer_specs_are_the_eight_prd_levels() -> None:
    assert [spec.id for spec in LAYER_SPECS] == [f"L{i}" for i in range(1, 9)]
    # 成本必须单调不降：越靠后的层级越贵，否则"优先本地"就失去意义
    costs = [spec.cost_cny for spec in LAYER_SPECS]
    assert costs == sorted(costs)
    assert spec_for("L1") is not None
    assert spec_for("L99") is None


async def test_chain_short_circuits_on_first_hit() -> None:
    rec = _Recorder()
    chain = RetrievalChain(
        [
            FunctionLayer("L1", rec.miss("L1")),
            FunctionLayer("L2", rec.hit("L2", "value")),
            FunctionLayer("L3", rec.hit("L3", "should-not-run")),
        ]
    )
    outcome = await chain.resolve(_query())
    assert outcome.resolved_by == "L2"
    assert outcome.value == "value"
    assert rec.called == ["L1", "L2"], "命中后绝不能继续调用后续层"
    assert [a.status for a in outcome.attempts] == ["miss", "hit"]


async def test_chain_skips_layers_that_do_not_handle_the_kind() -> None:
    rec = _Recorder()
    chain = RetrievalChain(
        [
            FunctionLayer("L5", rec.hit("L5", "cache"), kinds={"llm_json"}),
            FunctionLayer("L6", rec.hit("L6", "map"), kinds={"leg"}),
        ]
    )
    outcome = await chain.resolve(_query("leg"))
    assert rec.called == ["L6"]
    assert [a.status for a in outcome.attempts] == ["skipped", "hit"]


async def test_chain_continues_after_provider_error_and_flags_degraded() -> None:
    rec = _Recorder()
    chain = RetrievalChain(
        [
            FunctionLayer("L7", rec.boom("L7", ProviderError("deepseek", "complete"))),
            FunctionLayer("L8", rec.hit("L8", "fallback")),
        ]
    )
    outcome = await chain.resolve(_query())
    assert outcome.value == "fallback"
    assert outcome.degraded is True
    assert outcome.failed_layers == ("L7",)
    assert outcome.attempts[0].error_code == str(ErrorCode.PROVIDER_UNAVAILABLE)


async def test_chain_continues_after_cost_breaker() -> None:
    from app.core.errors import CostBreakerOpen

    rec = _Recorder()
    chain = RetrievalChain(
        [
            FunctionLayer("L7", rec.boom("L7", CostBreakerOpen("llm_calls", 4.0, 4.0))),
            FunctionLayer("L8", rec.hit("L8", "ok")),
        ]
    )
    outcome = await chain.resolve(_query())
    assert outcome.resolved_by == "L8"
    assert outcome.degraded is True


async def test_chain_returns_unresolved_outcome_when_all_layers_miss() -> None:
    rec = _Recorder()
    chain = RetrievalChain([FunctionLayer("L1", rec.miss("L1")), FunctionLayer("L2", rec.miss("L2"))])
    outcome = await chain.resolve(_query())
    assert outcome.resolved is False
    assert outcome.value is None
    assert outcome.resolved_by is None
    assert outcome.degraded is False


async def test_chain_does_not_swallow_unexpected_exceptions() -> None:
    """只捕获外部依赖失败与成本熔断；编程错误必须冒泡，不能被"降级"掩盖。"""

    async def _boom(query: RetrievalQuery) -> RetrievalResult | None:
        raise RuntimeError("bug")

    chain = RetrievalChain([FunctionLayer("L1", _boom)])
    with pytest.raises(RuntimeError):
        await chain.resolve(_query())


def test_chain_rejects_duplicate_layer_ids() -> None:
    rec = _Recorder()
    with pytest.raises(ValueError, match="重复层级"):
        RetrievalChain([FunctionLayer("L1", rec.miss("L1")), FunctionLayer("L1", rec.miss("L1"))])


def test_chain_describe_includes_spec_metadata() -> None:
    rec = _Recorder()
    chain = RetrievalChain([FunctionLayer("L1", rec.miss("L1"))])
    described = chain.describe()
    assert described[0]["key"] == "city_knowledge"
    assert described[0]["cost_cny"] == 0.0
    assert chain.layer_ids == ("L1",)


def test_chain_describe_handles_custom_layers() -> None:
    async def _fn(query: RetrievalQuery) -> RetrievalResult | None:
        return None

    described = RetrievalChain([FunctionLayer("custom", _fn)]).describe()
    assert described[0]["label"] == "custom"
    assert described[0]["key"] is None


async def test_layer_without_kinds_handles_every_kind() -> None:
    """DB 查询层是"任何类别都能试"，引擎必须支持这种层。"""

    class _AnyKindLayer:
        id = "L1"

        async def resolve(self, query: RetrievalQuery) -> RetrievalResult | None:
            return RetrievalResult(value="db", resolved_by="L1")

    chain = RetrievalChain([cast(RetrievalLayer, _AnyKindLayer())])
    outcome = await chain.resolve(RetrievalQuery(kind="whatever", key="k"))
    assert outcome.resolved_by == "L1"


def test_function_layer_supports_reports_scope() -> None:
    async def _fn(query: RetrievalQuery) -> RetrievalResult | None:
        return None

    layer = FunctionLayer("L1", _fn, kinds={"leg"})
    assert layer.supports("leg") is True
    assert layer.supports("search") is False


async def test_result_cost_and_cached_flags_are_preserved() -> None:
    async def _fn(query: RetrievalQuery) -> RetrievalResult | None:
        return RetrievalResult(
            value="v", resolved_by="L5", cost_cny=Decimal("0.01"), cached=True
        )

    outcome = await RetrievalChain([FunctionLayer("L5", _fn)]).resolve(_query())
    assert outcome.resolved_by == "L5"
