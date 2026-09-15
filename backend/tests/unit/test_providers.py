"""Provider 层单元测试（**不联网**，只测降级实现、纯函数与装配逻辑）。

真实 HTTP 行为在 ``tests/contract/test_provider_http.py`` 里用 respx 验证。
这里守住的是最重要的一条架构铁律：**缺少任何 Key 都必须能跑** ——
每个降级实现都要"如实不可用"，而不是抛一个让上层无法处理的异常，
更不是伪造数据。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast

import pytest
from pydantic import BaseModel

from app.core.config import (
    IMPLEMENTED_SEARCH_PROVIDERS,
    SEARCH_KEY_FIELDS,
    Settings,
    get_pricing_config,
)
from app.core.errors import ErrorCode, ProviderError
from app.providers.base import LatLng, ProviderHealth
from app.providers.llm.base import LlmMessage, LlmResponse, LlmTier, LlmUsage, complete_json
from app.providers.llm.deepseek import DeepSeekProvider
from app.providers.llm.null import NullLlmProvider
from app.providers.map.amap import AmapMapProvider
from app.providers.map.haversine import HaversineMapProvider
from app.providers.map.osrm import OsrmMapProvider
from app.providers.registry import build_providers
from app.providers.search.seed_only import SeedOnlyProvider
from app.providers.search.tavily import TavilyProvider
from app.providers.weather.base import WeatherThresholds, classify_weather, weather_from_daily
from app.providers.weather.null import NullWeatherProvider
from app.providers.weather.open_meteo import OpenMeteoWeatherProvider

pytestmark = pytest.mark.unit


# ── LLM 降级 + complete_json ────────────────────────────────────────────────


def test_provider_health_is_usable_flag() -> None:
    assert ProviderHealth(name="x", available=True).is_usable is True
    assert ProviderHealth(name="x", available=False).is_usable is False


async def test_null_llm_provider_is_honestly_unavailable() -> None:
    provider = NullLlmProvider()
    assert provider.health().available is False
    assert provider.health().degraded is True
    assert provider.model_for("fast").startswith("disabled")
    with pytest.raises(ProviderError) as excinfo:
        await provider.complete(
            [LlmMessage(role="user", content="hi")],
            tier="fast",
            max_output_tokens=10,
        )
    assert excinfo.value.code == ErrorCode.PROVIDER_UNAVAILABLE


class _Payload(BaseModel):
    answer: str


class _ScriptedLlm:
    """按脚本依次返回文本的假 LLM，用来测"校验失败重试 + 降级"。"""

    name = "scripted"

    def __init__(self, texts: Sequence[str]) -> None:
        self._texts = list(texts)
        self.calls = 0

    def model_for(self, tier: LlmTier) -> str:
        return "scripted"

    async def complete(
        self,
        messages: Sequence[LlmMessage],
        *,
        tier: LlmTier,
        max_output_tokens: int,
        temperature: float = 0.2,
        json_mode: bool = False,
    ) -> LlmResponse:
        self.calls += 1
        return LlmResponse(text=self._texts.pop(0), model="scripted", provider=self.name, usage=LlmUsage())


async def test_complete_json_parses_valid_object() -> None:
    provider = _ScriptedLlm(['{"answer": "ok"}'])
    result = await complete_json(
        cast(Any, provider), _Payload, [LlmMessage("user", "q")], tier="fast", max_output_tokens=10
    )
    assert result.answer == "ok"
    assert provider.calls == 1


async def test_complete_json_unwraps_markdown_fence() -> None:
    provider = _ScriptedLlm(["```json\n{\"answer\": \"fenced\"}\n```"])
    result = await complete_json(
        cast(Any, provider), _Payload, [LlmMessage("user", "q")], tier="fast", max_output_tokens=10
    )
    assert result.answer == "fenced"


async def test_complete_json_retries_then_succeeds() -> None:
    provider = _ScriptedLlm(["not json", '{"answer": "second"}'])
    result = await complete_json(
        cast(Any, provider), _Payload, [LlmMessage("user", "q")], tier="fast", max_output_tokens=10
    )
    assert result.answer == "second"
    assert provider.calls == 2


async def test_complete_json_rejects_non_object_json() -> None:
    provider = _ScriptedLlm(["[1, 2]", "[3]"])
    with pytest.raises(ProviderError) as excinfo:
        await complete_json(
            cast(Any, provider), _Payload, [LlmMessage("user", "q")], tier="fast", max_output_tokens=10
        )
    assert excinfo.value.code == ErrorCode.PROVIDER_INVALID_RESPONSE


async def test_complete_json_gives_up_after_retries() -> None:
    provider = _ScriptedLlm(["nope", "still nope"])
    with pytest.raises(ProviderError):
        await complete_json(
            cast(Any, provider),
            _Payload,
            [LlmMessage("user", "q")],
            tier="fast",
            max_output_tokens=10,
            retries=1,
        )
    assert provider.calls == 2


async def test_complete_json_propagates_provider_unavailable() -> None:
    """LLM 不可用是"降级"，不是"格式错" —— 上层据此走规则引擎，所以异常类型必须保留。"""
    with pytest.raises(ProviderError) as excinfo:
        await complete_json(
            NullLlmProvider(), _Payload, [LlmMessage("user", "q")], tier="fast", max_output_tokens=10
        )
    assert excinfo.value.code == ErrorCode.PROVIDER_UNAVAILABLE


# ── 搜索降级 ────────────────────────────────────────────────────────────────


async def test_seed_only_provider_never_fakes_results() -> None:
    provider = SeedOnlyProvider()
    assert provider.health().available is False
    assert await provider.search("广州 美食") == []
    assert provider.estimate_cost("search_basic") == 0
    verification = await provider.verify(cast(Any, None))
    assert verification.status == "unknown"
    with pytest.raises(ProviderError):
        await provider.extract("https://example.com", fields=["opening_hours"])


# ── 地图降级 ────────────────────────────────────────────────────────────────


async def test_haversine_provider_estimates_distance_and_duration() -> None:
    from app.core.config import TravelMode

    provider = HaversineMapProvider({"walk": TravelMode(speed_kmh=4.5, overhead_min=0)})
    leg = await provider.leg(LatLng(23.1291, 113.2644), LatLng(23.1371, 113.2644), mode="walk")
    assert leg.distance_source == "estimated"
    assert leg.duration_source == "estimated"
    assert leg.distance_m and leg.distance_m > 800
    assert leg.duration_min and leg.duration_min > 0
    assert provider.health().degraded is True


def test_haversine_provider_cost_is_zero() -> None:
    assert HaversineMapProvider({}).estimate_cost("route") == 0


async def test_haversine_provider_gives_no_duration_without_speed_assumption() -> None:
    """没有该出行方式的速度假设时只给距离 —— 宁可不给，也不猜。"""
    provider = HaversineMapProvider({})
    leg = await provider.leg(LatLng(23.1, 113.2), LatLng(23.2, 113.3), mode="ferry")
    assert leg.distance_m is not None
    assert leg.duration_min is None
    assert leg.duration_source is None


# ── 天气 ────────────────────────────────────────────────────────────────────


def test_classify_weather_prioritizes_rain_over_heat() -> None:
    assert classify_weather(rain_probability=0.9, temp_max_c=40) == "rain"
    assert classify_weather(rain_probability=0.1, temp_max_c=40) == "heat"
    assert classify_weather(rain_probability=0.1, temp_max_c=20) == "clear"


def test_classify_weather_respects_custom_thresholds() -> None:
    thresholds = WeatherThresholds(rain_probability=0.2, high_temp_c=50)
    assert classify_weather(rain_probability=0.25, temp_max_c=20, thresholds=thresholds) == "rain"
    assert classify_weather(rain_probability=0.1, temp_max_c=45, thresholds=thresholds) == "clear"


def test_weather_from_daily_maps_fields() -> None:
    snapshots = weather_from_daily(
        {
            "time": ["2026-09-11", "2026-09-12"],
            "precipitation_probability_max": [80, 10],
            "temperature_2m_max": [28, 36],
            "temperature_2m_min": [22, 27],
        }
    )
    assert [s.condition for s in snapshots] == ["rain", "heat"]
    assert snapshots[0].rain_probability == pytest.approx(0.8)
    assert snapshots[0].temp_min_c == 22


def test_weather_from_daily_skips_misaligned_rows() -> None:
    """数组长度不一致时不能错位配对（把明天的降雨概率配到后天）。"""
    snapshots = weather_from_daily(
        {
            "time": ["a", "b"],
            "precipitation_probability_max": [10],
            "temperature_2m_max": [20],
            "temperature_2m_min": [10, 11],
        }
    )
    assert len(snapshots) == 1
    assert snapshots[0].rain_probability == pytest.approx(0.1)


def test_weather_from_daily_rejects_non_sequence_date_field() -> None:
    """``time`` 是字符串时不逐字符迭代。

    open-meteo 参数写错时会返回 ``"time": "2026-09-12"``。字符串也是 ``Sequence``，
    ``enumerate`` 会把它拆成 ``"2"`` / ``"0"`` …… 并配上**真实的**温度，
    产出的是一批看起来像数据、实际日期全错的快照 —— 比直接报错危险得多。
    """
    assert weather_from_daily({"time": "2026-09-12", "temperature_2m_max": [30], "temperature_2m_min": [20]}) == []
    assert weather_from_daily({"time": b"2026-09-12", "temperature_2m_max": [30]}) == []
    assert weather_from_daily({"time": None}) == []
    assert weather_from_daily({}) == []


def test_weather_from_daily_ignores_non_numeric_values() -> None:
    snapshots = weather_from_daily(
        {
            "time": ["a", "b"],
            "temperature_2m_max": ["oops", 20],
            "temperature_2m_min": [10, 10],
        }
    )
    assert len(snapshots) == 1


# ── 计费单位只有一处真相 ─────────────────────────────────────────────────────


def test_tavily_credit_units_match_pricing_config() -> None:
    """Provider 里的 credit 数与 ``pricing.yaml`` 必须一致。

    "一次搜索算几个 credit"被写在了两个地方：Provider（厂商文档定义的事实）
    与配置（实际记账用的）。两者一旦漂移，估算与账单就会不一致 —— 而两边都
    "看起来是对的"。这条断言是它们之间唯一的连接。
    """
    costs = get_pricing_config().search["tavily"]["costs"]
    provider = TavilyProvider("sk-test")
    search_ops = {op: credits for op, credits in costs.items() if op.startswith("search_")}
    assert search_ops, "配置里必须列出搜索操作的 credit 数"
    for op, credits in search_ops.items():
        assert provider.estimate_cost(op) == credits, f"{op} 的 credit 数在配置与 Provider 之间不一致"


async def test_null_weather_returns_empty_forecast() -> None:
    provider = NullWeatherProvider()
    assert provider.health().degraded is True
    assert await provider.forecast(LatLng(23.1, 113.2)) == []


# ── 注册工厂 ────────────────────────────────────────────────────────────────


def _settings(**overrides: Any) -> Settings:
    """构造设置时**显式清空所有 Key**，单测因此不依赖开发机的 ``.env``。

    这是踩出来的坑：本机在 ``.env`` 里配了 ``DEEPSEEK_API_KEY`` 之后，
    下面“无 Key 时降级到 ``NullLlmProvider``”的用例立刻变红 ——
    它断言的是“没有 Key 的行为”，却隐式地把环境里的 Key 当成了输入。
    测试要的是确定性：环境变了，用例不应该跟着变。
    """
    base: dict[str, Any] = {
        "llm_provider": "deepseek",
        "deepseek_api_key": None,
        "openai_api_key": None,
        "anthropic_api_key": None,
        "search_provider": "auto",
        "tavily_api_key": None,
        "serper_api_key": None,
        "bing_search_api_key": None,
        "map_provider": "auto",
        "amap_web_key": None,
    }
    base.update(overrides)
    return Settings(**base)


def test_registry_defaults_to_degraded_but_runnable() -> None:
    providers = build_providers(_settings())
    assert isinstance(providers.llm, NullLlmProvider)
    assert isinstance(providers.search, SeedOnlyProvider)
    assert isinstance(providers.map, OsrmMapProvider)
    assert isinstance(providers.weather, OpenMeteoWeatherProvider)
    assert len(providers.degraded_modes()) >= 2, "无 Key 时至少 LLM 与搜索处于降级"


def test_registry_builds_real_providers_when_keys_present() -> None:
    providers = build_providers(
        _settings(
            llm_provider="deepseek",
            deepseek_api_key="sk-test",
            search_provider="tavily",
            tavily_api_key="tv-test",
            map_provider="amap",
            amap_web_key="amap-test",
        )
    )
    assert isinstance(providers.llm, DeepSeekProvider)
    assert isinstance(providers.search, TavilyProvider)
    assert isinstance(providers.map, AmapMapProvider)
    assert providers.degraded_modes() == [], "全部有 Key 时不应报降级（天气免费不算降级）"


@pytest.mark.parametrize("name", sorted(IMPLEMENTED_SEARCH_PROVIDERS))
def test_implemented_search_providers_say_and_do_the_same_thing(name: str) -> None:
    """★ 名单与工厂不许漂移 ★

    `IMPLEMENTED_SEARCH_PROVIDERS` 是「配置层说谁在跑」与「工厂层真跑谁」的单一事实源。
    这条测试对名单里每个名字同时断言两件事：配置层认它生效，且工厂真的构造出了它。
    只写其中一条（比如只断言 `search_provider_effective`）的话，
    工厂里漏掉一个分支照样是绿的。
    """
    settings = _settings(search_provider=name, **{SEARCH_KEY_FIELDS[name]: "test-key"})
    assert settings.search_provider_effective == name
    provider = build_providers(settings).search
    assert provider.name == name
    assert provider.health().degraded is False, "说已实现却报告降级，等于什么都没实现"


def test_unimplemented_search_keys_never_claim_to_be_effective() -> None:
    """可配但未实现的 Key（serper / bing）：不许谎报生效，且要说清 Key 被忽略了。"""
    for name, field in SEARCH_KEY_FIELDS.items():
        if name in IMPLEMENTED_SEARCH_PROVIDERS:
            continue
        settings = _settings(search_provider="auto", **{field: "test-key"})
        assert settings.search_provider_effective == "seed_only"
        providers = build_providers(settings)
        assert isinstance(providers.search, SeedOnlyProvider)
        assert any(mode.startswith(f"search:{name}(") for mode in providers.degraded_modes())


def test_registry_falls_back_to_haversine_and_null_weather() -> None:
    providers = build_providers(_settings(map_provider="haversine", weather_provider="disabled"))
    assert isinstance(providers.map, HaversineMapProvider)
    assert isinstance(providers.weather, NullWeatherProvider)


def test_registry_health_exposes_all_four_capabilities() -> None:
    health = build_providers(_settings()).health()
    assert set(health) == {"llm", "search", "map", "weather"}
    assert health["llm"].available is False


def test_registry_marks_amap_without_key_as_osrm_fallback() -> None:
    """显式要 amap 但没 Key 时降级到 osrm（真实路网），而不是崩溃。"""
    providers = build_providers(_settings(map_provider="amap"))
    assert isinstance(providers.map, OsrmMapProvider)
