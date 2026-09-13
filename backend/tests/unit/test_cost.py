"""成本层单元测试（PRD §15.4、FR-12）。

★ 这里最该守住的一条 ★
未校准的单价必须**如实标记**，不能被当成 0 元"美化"成本。
因此下面既有"calibrated=True 时金额算得对"的用例，也有
"单价是 null 时金额记 0 但 calibrated=False"的用例。
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.core.config import CircuitBreakerLimits, PricingConfig, get_limits_config, get_pricing_config
from app.core.errors import CostBreakerOpen
from app.services.cost import CostLedger, PriceBook, _decimal, _node

pytestmark = pytest.mark.unit


def _synthetic_pricing() -> PricingConfig:
    """一份"单价已校准"的合成配置，用来验证计算本身（真实配置里多数单价是 null）。"""
    return PricingConfig.model_validate(
        {
            "version": "test",
            "calibrated_at": "2026-01-01",
            "currency": "CNY",
            "sources": {},
            "fx": {"usd_cny": 7.2, "fx_updated_at": "2026-01-01"},
            "llm": {
                "deepseek": {
                    "fast": {
                        "input_per_mtok": 0.14,
                        "cached_input_per_mtok": 0.014,
                        "output_per_mtok": 0.28,
                    },
                    # 没有 cached 单价的档位，用于验证"回退到普通输入价"的分支
                    "strong": {"input_per_mtok": 0.55, "output_per_mtok": 2.19},
                }
            },
            "search": {
                "tavily": {
                    "credit_price_usd": 0.008,
                    "costs": {"search_basic": 1, "search_advanced": 2},
                    "needs_calibration": False,
                },
                # 按每千次查询计价的厂商（serper/bing 的形态）
                "fixed": {"price_per_1000_queries_usd": 5.0, "needs_calibration": False},
                # credit 单价缺失：必须判为未校准，而不是当成免费
                "broken_credit": {
                    "credit_price_usd": None,
                    "costs": {"search_basic": 1},
                    "needs_calibration": False,
                },
                # 固定单价写成了非数字 → 未知
                "weird": {"price_per_query_usd": "abc", "needs_calibration": False},
                "free": {"price_per_query_usd": 0, "needs_calibration": False},
                # 什么都没价：既无 credit、也无固定单价、也无每千次价 → 未知
                "no_price": {"needs_calibration": False},
            },
            "map": {
                "amap": {"route_per_call": 0.002, "needs_calibration": False},
                # 没有 route_per_call 这个操作键（瓦片按 per_call 计价）→ 未知
                "tiles": {"per_call": 0, "needs_calibration": False},
            },
            "weather": {
                "open_meteo": {"per_call": 0},
                # 未列价（无 per_call）→ 未知，不是免费
                "mobile": {"needs_calibration": False},
                # 收费厂商（用于验证金额与单价同币种）
                "charged": {"per_call": 0.001},
            },
            "budgets": {},
        }
    )


# ── PriceBook：校准单价下的计算 ─────────────────────────────────────────────


def test_llm_price_computes_tokens_times_rate() -> None:
    book = PriceBook(_synthetic_pricing())
    price = book.llm("deepseek", "fast", tokens_in=1000, tokens_out=500)
    assert price.calibrated is True
    # (1000*0.14 + 500*0.28) / 1e6 * 7.2 = 0.002016
    assert price.amount_cny == Decimal("0.002016")


def test_llm_price_uses_cached_rate_for_cached_tokens() -> None:
    book = PriceBook(_synthetic_pricing())
    price = book.llm("deepseek", "fast", tokens_in=1000, tokens_out=0, tokens_cached=400)
    # (600*0.14 + 400*0.014) / 1e6 * 7.2 = 0.00064512
    assert price.amount_cny == Decimal("0.000645")


def test_llm_price_falls_back_to_input_rate_when_cached_rate_missing() -> None:
    book = PriceBook(_synthetic_pricing())
    price = book.llm("deepseek", "strong", tokens_in=1000, tokens_out=0, tokens_cached=1000)
    # cached 单价缺失 → 全部按输入价（保守，宁可多算）
    assert price.amount_cny == book.llm("deepseek", "strong", tokens_in=1000, tokens_out=0).amount_cny
    assert price.calibrated is True


def test_llm_price_ignores_cached_tokens_larger_than_input() -> None:
    """脏数据（cached > input）不能算出负数金额。"""
    book = PriceBook(_synthetic_pricing())
    price = book.llm("deepseek", "fast", tokens_in=10, tokens_out=0, tokens_cached=999)
    assert price.amount_cny >= 0


def test_search_price_uses_credits() -> None:
    book = PriceBook(_synthetic_pricing())
    basic = book.search("tavily", "search_basic", units=1)
    advanced = book.search("tavily", "search_advanced", units=1)
    assert basic.calibrated is True
    assert basic.amount_cny == Decimal("0.057600")
    assert advanced.amount_cny == Decimal("0.115200")
    assert book.search("tavily", "search_basic", units=3).amount_cny == Decimal("0.172800")


def test_search_price_per_1000_queries() -> None:
    book = PriceBook(_synthetic_pricing())
    price = book.search("fixed", "search_basic", units=2000)
    assert price.calibrated is True
    assert price.amount_cny == Decimal("72.000000")  # 2 * 5 USD * 7.2


def test_search_broken_credit_setup_is_uncalibrated() -> None:
    book = PriceBook(_synthetic_pricing())
    assert book.search("broken_credit").calibrated is False
    assert book.search("no_price").calibrated is False


def test_search_non_numeric_flat_price_is_uncalibrated() -> None:
    book = PriceBook(_synthetic_pricing())
    assert book.search("weird").calibrated is False
    assert book.search("free").amount_cny == Decimal("0")
    assert book.search("free").calibrated is True


def test_map_missing_operation_key_is_uncalibrated() -> None:
    book = PriceBook(_synthetic_pricing())
    price = book.map("tiles", "route_per_call")
    assert price.calibrated is False


def test_weather_missing_per_call_is_uncalibrated() -> None:
    book = PriceBook(_synthetic_pricing())
    assert book.weather("mobile").calibrated is False


def test_map_and_weather_prices() -> None:
    book = PriceBook(_synthetic_pricing())
    leg = book.map("amap", "route_per_call", units=2)
    assert leg.calibrated is True
    assert leg.amount_cny == Decimal("0.028800")
    weather = book.weather("open_meteo")
    assert weather.calibrated is True
    assert weather.amount_cny == Decimal("0")


def test_unit_price_is_in_cny_for_map_and_weather() -> None:
    """``unit_price`` 必须与 ``amount_cny`` 同币种。

    两者都是从同一份美元单价的配置算出来的；若只换金额不换单价，
    ``cost_logs`` 里就会同时存在两种口径，报表按单价聚合时会得到差一个汇率的数字。
    """
    book = PriceBook(_synthetic_pricing())
    amap = book.map("amap", "route_per_call", units=1)
    # 合成配置：0.002 USD/次 × 7.2
    assert amap.amount_cny == Decimal("0.014400")
    assert amap.unit_price_cny == Decimal("0.01440000")
    assert amap.amount_cny == amap.unit_price_cny, "单次调用的金额与单价必须相等"

    weather = book.weather("charged")
    assert weather.amount_cny == Decimal("0.007200")
    assert weather.amount_cny == weather.unit_price_cny


# ── PriceBook：未校准 / 未知单价必须如实标记 ────────────────────────────────


def test_real_config_deepseek_is_calibrated_to_the_written_unit_price() -> None:
    """真实配置里的 DeepSeek 单价已被校准，金额必须就是 YAML 里那个数。

    2026-09-13 从官方价目页填入（deepseek-flash 高峰价，美元/百万 token）：
    input 0.30 / cached 0.006 / output 1.20，汇率 7.20。

    写死这个数字是**故意的**：如果有人悄悄改了 pricing.yaml，这里会立刻红，
    而不是让成本报表在一个没人注意的地方慢慢跑偏。
    """
    book = PriceBook(get_pricing_config())
    price = book.llm("deepseek", "fast", tokens_in=1000, tokens_out=500)

    # (1000×0.30 + 500×1.20) / 1e6 × 7.20
    assert price.amount_cny == Decimal("0.00648")
    assert price.calibrated is True


def test_real_config_marks_unknown_llm_price_as_unknown_not_free() -> None:
    """未填单价的厂商（openai / anthropic 预留档）金额记 0，但必须标记未校准。"""
    book = PriceBook(get_pricing_config())
    price = book.llm("openai", "fast", tokens_in=1000, tokens_out=500)
    assert price.amount_cny == Decimal("0")
    assert price.calibrated is False


def test_real_config_seed_only_is_free_and_calibrated() -> None:
    book = PriceBook(get_pricing_config())
    price = book.search("seed_only", "search_basic", units=5)
    assert price.amount_cny == Decimal("0")
    assert price.calibrated is True, "0 元是已知事实，不是未知"


def test_real_config_tavily_is_calibrated() -> None:
    book = PriceBook(get_pricing_config())
    assert book.search("tavily", "search_basic").calibrated is True


def test_real_config_amap_is_uncalibrated() -> None:
    book = PriceBook(get_pricing_config())
    assert book.map("amap").calibrated is False


def test_real_config_serper_is_uncalibrated() -> None:
    book = PriceBook(get_pricing_config())
    assert book.search("serper").calibrated is False


def test_unknown_provider_is_uncalibrated() -> None:
    book = PriceBook(get_pricing_config())
    assert book.search("nonexistent").calibrated is False
    assert book.map("nonexistent").calibrated is False
    assert book.weather("nonexistent").calibrated is False


def test_llm_unknown_tier_is_uncalibrated() -> None:
    book = PriceBook(get_pricing_config())
    assert book.llm("deepseek", "nonexistent").calibrated is False


def test_node_and_decimal_helpers_tolerate_bad_values() -> None:
    assert _node({"a": {"b": 1}}, "a") == {"b": 1}
    assert _node({"a": 1}, "a", "b") == {}
    assert _node({"a": 1}, "a") == {}
    assert _node({}, "a") == {}
    assert _decimal(None) is None
    assert _decimal(True) is None
    assert _decimal("abc") is None
    assert _decimal("1.5") == Decimal("1.5")
    assert _decimal({"x": 1}) is None


# ── CostLedger ──────────────────────────────────────────────────────────────


def _ledger() -> CostLedger:
    limits = get_limits_config()
    return CostLedger(
        price_book=PriceBook(get_pricing_config()),
        breaker=limits.cost.circuit_breaker,
        providers=limits.providers,
    )


def test_ledger_records_and_sums_by_category() -> None:
    ledger = _ledger()
    from app.services.cost import Price

    ledger.record("search", "tavily", price=Price(amount_cny=Decimal("0.06"), calibrated=True))
    ledger.record("map", "osrm", price=Price(amount_cny=Decimal("0"), calibrated=True))
    assert ledger.spent_cny() == Decimal("0.06")
    assert ledger.spent_cny("search") == Decimal("0.06")
    assert ledger.calls("search") == 1
    assert ledger.calls("map") == 1
    assert ledger.all_calibrated is True


def test_ledger_cache_hit_is_recorded_but_not_counted_as_a_call() -> None:
    ledger = _ledger()
    ledger.record_cache_hit("llm", "deepseek", operation="intent_parse")
    assert ledger.calls("llm") == 0, "缓存命中不是外部调用，否则熔断会被误触发"
    assert ledger.entries[0].cache_hit is True
    assert ledger.spent_cny() == Decimal("0")


def test_ledger_flags_uncalibrated_entries() -> None:
    """只要有**一条**未校准的账，整次规划就不能声称成本已校准。

    用高德（map.amap 仍未取官方价目）来构造：DeepSeek 已于 2026-09-13 校准，
    拿它当反例会让这个测试名不副实。
    """
    ledger = _ledger()
    price = ledger.price_book.map("amap")
    ledger.record("map", "amap", price=price)
    assert ledger.all_calibrated is False
    assert ledger.summary()["pricing_calibrated"] is False


def test_ledger_llm_call_breaker_counts_calls_not_money() -> None:
    """单价未校准时金额恒为 0，熔断只能靠次数 —— 这正是它存在的意义。"""
    ledger = _ledger()
    from app.services.cost import Price

    limit = ledger.breaker.plan_llm_calls
    for _ in range(limit):
        ledger.ensure_allowed("llm")
        ledger.record("llm", "deepseek", price=Price(amount_cny=Decimal("0"), calibrated=False))
    with pytest.raises(CostBreakerOpen) as excinfo:
        ledger.ensure_allowed("llm")
    assert excinfo.value.context["kind"] == "llm_calls"


def test_ledger_map_call_breaker() -> None:
    ledger = _ledger()
    ledger.breaker = CircuitBreakerLimits(
        plan_total_cny=999.0, plan_search_cny=999.0, plan_map_calls=1, plan_llm_calls=99
    )
    from app.services.cost import Price

    ledger.ensure_allowed("map")
    ledger.record("map", "osrm", price=Price(amount_cny=Decimal("0"), calibrated=True))
    with pytest.raises(CostBreakerOpen):
        ledger.ensure_allowed("map")


def test_ledger_search_query_count_breaker() -> None:
    ledger = _ledger()
    ledger.breaker = CircuitBreakerLimits(
        plan_total_cny=999.0, plan_search_cny=999.0, plan_map_calls=99, plan_llm_calls=99
    )
    from app.services.cost import Price

    for _ in range(ledger.providers.search_max_queries_per_plan):
        ledger.record("search", "tavily", price=Price(amount_cny=Decimal("0"), calibrated=True))
    with pytest.raises(CostBreakerOpen):
        ledger.ensure_allowed("search")


def test_ledger_search_cost_breaker() -> None:
    ledger = _ledger()
    ledger.breaker = CircuitBreakerLimits(
        plan_total_cny=999.0, plan_search_cny=0.01, plan_map_calls=99, plan_llm_calls=99
    )
    from app.services.cost import Price

    ledger.record("search", "tavily", price=Price(amount_cny=Decimal("0.05"), calibrated=True))
    with pytest.raises(CostBreakerOpen) as excinfo:
        ledger.ensure_allowed("search")
    assert excinfo.value.context["kind"] == "search"


def test_ledger_total_cost_breaker_beats_everything() -> None:
    ledger = _ledger()
    ledger.breaker = CircuitBreakerLimits(
        plan_total_cny=0.01, plan_search_cny=999.0, plan_map_calls=99, plan_llm_calls=99
    )
    from app.services.cost import Price

    ledger.record("llm", "deepseek", price=Price(amount_cny=Decimal("0.05"), calibrated=True))
    with pytest.raises(CostBreakerOpen) as excinfo:
        ledger.ensure_allowed("plan")
    assert excinfo.value.context["kind"] == "plan_total"


def test_ledger_plan_kind_is_noop_when_within_budget() -> None:
    ledger = _ledger()
    ledger.ensure_allowed("plan")  # 不抛错即为通过


def test_ledger_summary_shape() -> None:
    ledger = _ledger()
    from app.services.cost import Price

    ledger.record("llm", "deepseek", price=Price(amount_cny=Decimal("0.01"), calibrated=True), model="m")
    ledger.record_cache_hit("search", "tavily")
    summary = ledger.summary()
    assert set(summary["by_category"]) == {"llm", "search", "map", "other"}
    assert summary["calls"]["llm"] == 1
    assert summary["cache_hits"] == 1


def test_circuit_breaker_limits_require_all_fields() -> None:
    with pytest.raises(ValidationError):
        CircuitBreakerLimits(plan_total_cny=1.0)  # type: ignore[call-arg]
