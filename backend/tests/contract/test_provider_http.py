"""Provider HTTP 契约测试（respx 拦截，**不发真实网络请求**）。

为什么单独一层（PRD §23.3 把 providers 的契约测试单列）：
真实 Provider 的错误处理是"降级矩阵"的入口 —— 超时、5xx、非 JSON、
结构缺字段，每一种都必须被翻译成正确的 ``ProviderError.kind``，
否则上层会做出错误的降级决定（比如把"模型抽风"当成"没配 Key"）。

这里只验证请求形状与响应翻译。**真实 Key 下的端到端调用不在自动化测试里**
（避免产生费用与不确定性），由 M3 的手工校准流程覆盖。
"""

from __future__ import annotations

import httpx
import pytest
import respx

from app.core.errors import ErrorCode, ProviderError
from app.providers.base import LatLng
from app.providers.llm.base import LlmMessage
from app.providers.llm.deepseek import DeepSeekProvider
from app.providers.map.amap import AmapMapProvider
from app.providers.map.osrm import OsrmMapProvider
from app.providers.search.tavily import TavilyProvider
from app.providers.weather.open_meteo import OpenMeteoWeatherProvider

pytestmark = pytest.mark.contract

DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
TAVILY_URL = "https://api.tavily.com/search"
# OSRM 的 URL 里带 profile：walk → foot，taxi → driving（公共实例只有 car profile，见 geo.py）
OSRM_FOOT_URL = "https://router.project-osrm.org/route/v1/foot/113.200000,23.100000;113.300000,23.200000"
OSRM_DRIVING_URL = (
    "https://router.project-osrm.org/route/v1/driving/113.200000,23.100000;113.300000,23.200000"
)
AMAP_WALK_URL = "https://restapi.amap.com/v3/direction/walking"
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"


def _messages() -> list[LlmMessage]:
    return [LlmMessage(role="user", content="hi")]


# ── DeepSeek ────────────────────────────────────────────────────────────────


@respx.mock
async def test_deepseek_parses_completion_and_usage() -> None:
    respx.post(DEEPSEEK_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "model": "deepseek-chat",
                "choices": [{"message": {"content": "广州天气不错"}}],
                "usage": {
                    "prompt_tokens": 120,
                    "completion_tokens": 30,
                    "prompt_cache_hit_tokens": 80,
                },
            },
        )
    )
    provider = DeepSeekProvider("sk-test")
    response = await provider.complete(_messages(), tier="fast", max_output_tokens=128)
    assert response.text == "广州天气不错"
    assert response.model == "deepseek-chat"
    assert response.usage.tokens_in == 120
    assert response.usage.tokens_cached == 80
    assert provider.health().available is True


@respx.mock
async def test_deepseek_missing_usage_is_marked_unknown() -> None:
    """响应里没有 ``usage``（有些 OpenAI 兼容网关会省略）⇒ 用量未知，不是 0。

    把它当成 0 token 会生成一条 ``calibrated=True`` 的 0 元记录，
    等于向用户声称"这次调用没花钱"，而实际上我们只是不知道花了多少。
    """
    respx.post(DEEPSEEK_URL).mock(
        return_value=httpx.Response(
            200, json={"model": "deepseek-chat", "choices": [{"message": {"content": "hi"}}]}
        )
    )
    response = await DeepSeekProvider("sk-test").complete(_messages(), tier="fast", max_output_tokens=16)
    assert response.usage.usage_known is False
    assert response.usage.tokens_in == 0


@respx.mock
async def test_deepseek_tolerates_dirty_usage_counters() -> None:
    """``usage`` 是外部输入：字符串 / null / 负数都不能把一次成功调用变成异常。"""
    respx.post(DEEPSEEK_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "model": "deepseek-chat",
                "choices": [{"message": {"content": "hi"}}],
                "usage": {
                    "prompt_tokens": "120",
                    "completion_tokens": None,
                    "prompt_cache_hit_tokens": "n/a",
                },
            },
        )
    )
    provider = DeepSeekProvider("sk-test")
    response = await provider.complete(_messages(), tier="fast", max_output_tokens=16)
    assert response.usage.usage_known is True, "usage 字段在，只是计数脏"
    assert response.usage.tokens_in == 120
    assert response.usage.tokens_out == 0
    assert response.usage.tokens_cached == 0

    # 小数 / 布尔 / 根本不是数字的容器，都只能回退到 0（不能抛）
    respx.post(DEEPSEEK_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "model": "deepseek-chat",
                "choices": [{"message": {"content": "hi"}}],
                "usage": {
                    "prompt_tokens": 120.9,
                    "completion_tokens": [1, 2],
                    "prompt_cache_hit_tokens": True,
                },
            },
        )
    )
    response = await provider.complete(_messages(), tier="fast", max_output_tokens=16)
    assert response.usage.tokens_in == 120
    assert response.usage.tokens_out == 0
    assert response.usage.tokens_cached == 0


@respx.mock
async def test_deepseek_retries_on_5xx_then_succeeds() -> None:
    route = respx.post(DEEPSEEK_URL)
    route.side_effect = [
        httpx.Response(500, json={"error": "boom"}),
        httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]}),
    ]
    provider = DeepSeekProvider("sk-test", max_retries=1)
    response = await provider.complete(_messages(), tier="fast", max_output_tokens=16)
    assert response.text == "ok"
    assert route.call_count == 2


@respx.mock
async def test_deepseek_does_not_retry_on_4xx() -> None:
    route = respx.post(DEEPSEEK_URL).mock(return_value=httpx.Response(401, json={}))
    provider = DeepSeekProvider("sk-test", max_retries=3)
    with pytest.raises(ProviderError) as excinfo:
        await provider.complete(_messages(), tier="fast", max_output_tokens=16)
    assert excinfo.value.code == ErrorCode.PROVIDER_UNAVAILABLE
    assert route.call_count == 1, "4xx 是请求本身的问题，重试没有意义"


@respx.mock
async def test_deepseek_reports_invalid_structure() -> None:
    respx.post(DEEPSEEK_URL).mock(return_value=httpx.Response(200, json={"nope": True}))
    with pytest.raises(ProviderError) as excinfo:
        await DeepSeekProvider("sk-test").complete(_messages(), tier="fast", max_output_tokens=16)
    assert excinfo.value.code == ErrorCode.PROVIDER_INVALID_RESPONSE


@respx.mock
async def test_deepseek_reports_empty_content_as_invalid() -> None:
    respx.post(DEEPSEEK_URL).mock(
        return_value=httpx.Response(200, json={"choices": [{"message": {"content": ""}}]})
    )
    with pytest.raises(ProviderError) as excinfo:
        await DeepSeekProvider("sk-test").complete(_messages(), tier="fast", max_output_tokens=16)
    assert excinfo.value.code == ErrorCode.PROVIDER_INVALID_RESPONSE


@respx.mock
async def test_deepseek_timeout_is_reported_as_timeout() -> None:
    respx.post(DEEPSEEK_URL).mock(side_effect=httpx.ConnectTimeout("slow"))
    with pytest.raises(ProviderError) as excinfo:
        await DeepSeekProvider("sk-test", max_retries=0).complete(
            _messages(), tier="fast", max_output_tokens=16
        )
    assert excinfo.value.code == ErrorCode.PROVIDER_TIMEOUT


def test_deepseek_requires_api_key() -> None:
    with pytest.raises(ValueError):
        DeepSeekProvider("")


# ── Tavily ──────────────────────────────────────────────────────────────────


@respx.mock
async def test_tavily_parses_results_and_derives_domain() -> None:
    respx.post(TAVILY_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {"title": "广州文旅", "url": "https://gz.gov.cn/a", "content": "免费", "score": 0.9},
                    {"title": "缺 URL", "content": "x"},
                    "not-a-dict",
                ]
            },
        )
    )
    results = await TavilyProvider("tv-test").search("广州 免费 景点")
    assert len(results) == 1
    assert results[0].domain == "gz.gov.cn"
    assert results[0].score == 0.9


@respx.mock
async def test_tavily_passes_filters() -> None:
    route = respx.post(TAVILY_URL).mock(return_value=httpx.Response(200, json={"results": []}))
    await TavilyProvider("tv-test").search(
        "x", max_results=3, domains=["gz.gov.cn"], recency_days=10
    )
    body = route.calls[0].request.content
    assert b"gz.gov.cn" in body
    assert b"days" in body


@respx.mock
async def test_tavily_timeout_is_reported_as_timeout() -> None:
    respx.post(TAVILY_URL).mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(ProviderError) as excinfo:
        await TavilyProvider("tv-test").search("x")
    assert excinfo.value.code == ErrorCode.PROVIDER_TIMEOUT


@respx.mock
async def test_tavily_network_error_and_non_object_body() -> None:
    respx.post(TAVILY_URL).mock(side_effect=httpx.ConnectError("dns"))
    with pytest.raises(ProviderError) as excinfo:
        await TavilyProvider("tv-test").search("x")
    assert excinfo.value.code == ErrorCode.PROVIDER_UNAVAILABLE

    respx.post(TAVILY_URL).mock(return_value=httpx.Response(200, json=[1, 2]))
    with pytest.raises(ProviderError) as excinfo:
        await TavilyProvider("tv-test").search("x")
    assert excinfo.value.code == ErrorCode.PROVIDER_INVALID_RESPONSE


@respx.mock
async def test_tavily_missing_results_key_yields_empty_list() -> None:
    respx.post(TAVILY_URL).mock(return_value=httpx.Response(200, json={"answer": "no results key"}))
    assert await TavilyProvider("tv-test").search("x") == []


@respx.mock
async def test_tavily_recency_bucket_and_empty_urls() -> None:
    route = respx.post(TAVILY_URL).mock(
        return_value=httpx.Response(200, json={"results": [{"title": "无链接", "url": "  "}]})
    )
    results = await TavilyProvider("tv-test").search("x", recency_days=99)
    assert results == [], "没有 URL 的结果无法追溯来源，必须丢弃"
    assert b'"days":30' in route.calls[0].request.content, "超出档位的天数归到 30 天"


@respx.mock
async def test_tavily_http_error_and_non_json() -> None:
    respx.post(TAVILY_URL).mock(return_value=httpx.Response(503, text="down"))
    with pytest.raises(ProviderError) as excinfo:
        await TavilyProvider("tv-test").search("x")
    assert excinfo.value.code == ErrorCode.PROVIDER_UNAVAILABLE

    respx.post(TAVILY_URL).mock(return_value=httpx.Response(200, text="<html>"))
    with pytest.raises(ProviderError) as excinfo:
        await TavilyProvider("tv-test").search("x")
    assert excinfo.value.code == ErrorCode.PROVIDER_INVALID_RESPONSE


@respx.mock
async def test_tavily_verify_confirms_and_conflicts() -> None:
    respx.post(TAVILY_URL).mock(
        return_value=httpx.Response(
            200,
            json={"results": [{"title": "开放时间 09:00-17:00", "url": "https://a", "content": ""}]},
        )
    )
    provider = TavilyProvider("tv-test")
    from app.providers.search.base import Claim

    confirmed = await provider.verify(Claim(subject="陈家祠", field="opening_hours", value="09:00-17:00"))
    assert confirmed.status == "confirmed"
    conflicting = await provider.verify(Claim(subject="陈家祠", field="opening_hours", value="08:00-18:00"))
    assert conflicting.status == "conflicting"


@respx.mock
async def test_tavily_verify_without_results_is_unknown() -> None:
    respx.post(TAVILY_URL).mock(return_value=httpx.Response(200, json={"results": []}))
    from app.providers.search.base import Claim

    result = await TavilyProvider("tv-test").verify(Claim(subject="x", field="y", value="z"))
    assert result.status == "unknown"


async def test_tavily_extract_is_explicitly_unimplemented() -> None:
    with pytest.raises(ProviderError):
        await TavilyProvider("tv-test").extract("https://x", fields=["opening_hours"])


def test_tavily_estimate_cost_uses_credits() -> None:
    provider = TavilyProvider("tv-test")
    assert provider.estimate_cost("search_basic", 3) == 3
    assert provider.estimate_cost("search_advanced", 2) == 4
    assert provider.estimate_cost("unknown_op") == 1


def test_tavily_requires_api_key() -> None:
    with pytest.raises(ValueError):
        TavilyProvider("")


# ── OSRM ────────────────────────────────────────────────────────────────────


@respx.mock
async def test_osrm_returns_distance_but_not_walking_duration() -> None:
    """公共实例只有 car profile：步行距离可信，步行耗时不可信（必须为 None）。"""
    respx.get(OSRM_FOOT_URL).mock(
        return_value=httpx.Response(200, json={"code": "Ok", "routes": [{"distance": 2349.0, "duration": 189.6}]})
    )
    leg = await OsrmMapProvider().leg(LatLng(23.1, 113.2), LatLng(23.2, 113.3), mode="walk")
    assert leg.distance_m == 2349
    assert leg.distance_source == "osrm"
    assert leg.duration_min is None, "把车速当步行时间会得出荒谬结论"


@respx.mock
async def test_osrm_trusts_duration_for_car_mode() -> None:
    respx.get(OSRM_DRIVING_URL).mock(
        return_value=httpx.Response(200, json={"code": "Ok", "routes": [{"distance": 6000.0, "duration": 600.0}]})
    )
    leg = await OsrmMapProvider().leg(LatLng(23.1, 113.2), LatLng(23.2, 113.3), mode="taxi")
    assert leg.duration_min == 10
    assert leg.duration_source == "osrm"


@respx.mock
async def test_osrm_maps_error_codes_and_missing_fields() -> None:
    respx.get(OSRM_FOOT_URL).mock(return_value=httpx.Response(200, json={"code": "NoRoute", "routes": []}))
    with pytest.raises(ProviderError) as excinfo:
        await OsrmMapProvider().leg(LatLng(23.1, 113.2), LatLng(23.2, 113.3), mode="walk")
    assert excinfo.value.code == ErrorCode.PROVIDER_INVALID_RESPONSE

    respx.get(OSRM_FOOT_URL).mock(
        return_value=httpx.Response(200, json={"code": "Ok", "routes": [{"duration": 1}]})
    )
    with pytest.raises(ProviderError):
        await OsrmMapProvider().leg(LatLng(23.1, 113.2), LatLng(23.2, 113.3), mode="walk")


@respx.mock
async def test_osrm_taxi_duration_rounds_up() -> None:
    """89 秒必须算 2 分钟，不能四舍五入成 1 分钟。

    与高德、以及 ``domain.geo.travel_minutes`` 同一政策：宁可多算 1 分钟，
    也不要让用户以为"来得及"（少算的那一分钟是用户的时间）。下界是 1 分钟，
    避免"距离 200 米、耗时 0 分钟"这种无意义的输出。
    """
    respx.get(OSRM_DRIVING_URL).mock(
        return_value=httpx.Response(200, json={"code": "Ok", "routes": [{"distance": 500.0, "duration": 89.0}]})
    )
    leg = await OsrmMapProvider().leg(LatLng(23.1, 113.2), LatLng(23.2, 113.3), mode="taxi")
    assert leg.duration_min == 2

    respx.get(OSRM_DRIVING_URL).mock(
        return_value=httpx.Response(200, json={"code": "Ok", "routes": [{"distance": 10.0, "duration": 5.0}]})
    )
    leg = await OsrmMapProvider().leg(LatLng(23.1, 113.2), LatLng(23.2, 113.3), mode="taxi")
    assert leg.duration_min == 1


def test_osrm_cost_is_zero() -> None:
    assert OsrmMapProvider().estimate_cost("route") == 0


@respx.mock
async def test_osrm_ok_with_empty_routes_is_invalid() -> None:
    respx.get(OSRM_FOOT_URL).mock(return_value=httpx.Response(200, json={"code": "Ok", "routes": []}))
    with pytest.raises(ProviderError) as excinfo:
        await OsrmMapProvider().leg(LatLng(23.1, 113.2), LatLng(23.2, 113.3), mode="walk")
    assert excinfo.value.code == ErrorCode.PROVIDER_INVALID_RESPONSE


@respx.mock
async def test_osrm_network_error_and_non_object_body() -> None:
    respx.get(OSRM_FOOT_URL).mock(side_effect=httpx.ConnectError("dns"))
    with pytest.raises(ProviderError) as excinfo:
        await OsrmMapProvider().leg(LatLng(23.1, 113.2), LatLng(23.2, 113.3), mode="walk")
    assert excinfo.value.code == ErrorCode.PROVIDER_UNAVAILABLE

    respx.get(OSRM_FOOT_URL).mock(return_value=httpx.Response(200, json=[1, 2]))
    with pytest.raises(ProviderError) as excinfo:
        await OsrmMapProvider().leg(LatLng(23.1, 113.2), LatLng(23.2, 113.3), mode="walk")
    assert excinfo.value.code == ErrorCode.PROVIDER_INVALID_RESPONSE


@respx.mock
async def test_osrm_handles_transport_failures() -> None:
    respx.get(OSRM_FOOT_URL).mock(return_value=httpx.Response(500, text="err"))
    with pytest.raises(ProviderError) as excinfo:
        await OsrmMapProvider().leg(LatLng(23.1, 113.2), LatLng(23.2, 113.3), mode="walk")
    assert excinfo.value.code == ErrorCode.PROVIDER_UNAVAILABLE

    respx.get(OSRM_FOOT_URL).mock(return_value=httpx.Response(200, text="<html>"))
    with pytest.raises(ProviderError) as excinfo:
        await OsrmMapProvider().leg(LatLng(23.1, 113.2), LatLng(23.2, 113.3), mode="walk")
    assert excinfo.value.code == ErrorCode.PROVIDER_INVALID_RESPONSE

    respx.get(OSRM_FOOT_URL).mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(ProviderError) as excinfo:
        await OsrmMapProvider().leg(LatLng(23.1, 113.2), LatLng(23.2, 113.3), mode="walk")
    assert excinfo.value.code == ErrorCode.PROVIDER_TIMEOUT


# ── 高德 ────────────────────────────────────────────────────────────────────


@respx.mock
async def test_amap_parses_walking_result() -> None:
    respx.get(AMAP_WALK_URL).mock(
        return_value=httpx.Response(
            200,
            json={"status": "1", "info": "OK", "route": {"paths": [{"distance": "2600", "duration": "1830"}]}},
        )
    )
    leg = await AmapMapProvider("amap-test").leg(LatLng(23.1, 113.2), LatLng(23.2, 113.3), mode="walk")
    assert leg is not None
    assert leg.distance_m == 2600
    assert leg.duration_min == 31  # 1830 秒向上取整到 31 分钟
    assert leg.distance_source == "amap"
    assert leg.duration_source == "amap"


@respx.mock
async def test_amap_returns_none_for_unsupported_modes_without_requesting() -> None:
    route = respx.get(AMAP_WALK_URL).mock(return_value=httpx.Response(200, json={}))
    provider = AmapMapProvider("amap-test")
    for mode in ("metro", "bus", "ferry"):
        assert await provider.leg(LatLng(23.1, 113.2), LatLng(23.2, 113.3), mode=mode) is None
    assert route.call_count == 0


@respx.mock
async def test_amap_reports_business_error() -> None:
    respx.get(AMAP_WALK_URL).mock(
        return_value=httpx.Response(200, json={"status": "0", "info": "INVALID_USER_KEY"})
    )
    with pytest.raises(ProviderError) as excinfo:
        await AmapMapProvider("amap-test").leg(LatLng(23.1, 113.2), LatLng(23.2, 113.3), mode="walk")
    assert excinfo.value.code == ErrorCode.PROVIDER_INVALID_RESPONSE


@respx.mock
async def test_amap_reports_missing_fields() -> None:
    respx.get(AMAP_WALK_URL).mock(return_value=httpx.Response(200, json={"status": "1"}))
    with pytest.raises(ProviderError):
        await AmapMapProvider("amap-test").leg(LatLng(23.1, 113.2), LatLng(23.2, 113.3), mode="walk")

    respx.get(AMAP_WALK_URL).mock(
        return_value=httpx.Response(200, json={"status": "1", "route": {"paths": [{"duration": "60"}]}})
    )
    with pytest.raises(ProviderError):
        await AmapMapProvider("amap-test").leg(LatLng(23.1, 113.2), LatLng(23.2, 113.3), mode="walk")


@respx.mock
async def test_amap_network_error_and_non_object_body() -> None:
    respx.get(AMAP_WALK_URL).mock(side_effect=httpx.ConnectError("dns"))
    with pytest.raises(ProviderError) as excinfo:
        await AmapMapProvider("amap-test").leg(LatLng(23.1, 113.2), LatLng(23.2, 113.3), mode="walk")
    assert excinfo.value.code == ErrorCode.PROVIDER_UNAVAILABLE

    respx.get(AMAP_WALK_URL).mock(return_value=httpx.Response(200, json=[1, 2]))
    with pytest.raises(ProviderError) as excinfo:
        await AmapMapProvider("amap-test").leg(LatLng(23.1, 113.2), LatLng(23.2, 113.3), mode="walk")
    assert excinfo.value.code == ErrorCode.PROVIDER_INVALID_RESPONSE


@respx.mock
async def test_amap_accepts_numeric_fields() -> None:
    """高德文档说字段是字符串，但真实响应里数字/字符串都出现过 —— 两种都要能解析。"""
    respx.get(AMAP_WALK_URL).mock(
        return_value=httpx.Response(
            200,
            json={"status": "1", "route": {"paths": [{"distance": 2600, "duration": 1800}]}},
        )
    )
    leg = await AmapMapProvider("amap-test").leg(LatLng(23.1, 113.2), LatLng(23.2, 113.3), mode="walk")
    assert leg is not None
    assert leg.distance_m == 2600
    assert leg.duration_min == 30


def test_amap_cost_is_placeholder_zero() -> None:
    assert AmapMapProvider("amap-test").estimate_cost("direction") == 0


@respx.mock
async def test_amap_handles_transport_failures() -> None:
    respx.get(AMAP_WALK_URL).mock(return_value=httpx.Response(500, text="err"))
    with pytest.raises(ProviderError) as excinfo:
        await AmapMapProvider("amap-test").leg(LatLng(23.1, 113.2), LatLng(23.2, 113.3), mode="walk")
    assert excinfo.value.code == ErrorCode.PROVIDER_UNAVAILABLE

    respx.get(AMAP_WALK_URL).mock(return_value=httpx.Response(200, text="oops"))
    with pytest.raises(ProviderError) as excinfo:
        await AmapMapProvider("amap-test").leg(LatLng(23.1, 113.2), LatLng(23.2, 113.3), mode="walk")
    assert excinfo.value.code == ErrorCode.PROVIDER_INVALID_RESPONSE

    respx.get(AMAP_WALK_URL).mock(side_effect=httpx.ConnectTimeout("slow"))
    with pytest.raises(ProviderError) as excinfo:
        await AmapMapProvider("amap-test").leg(LatLng(23.1, 113.2), LatLng(23.2, 113.3), mode="walk")
    assert excinfo.value.code == ErrorCode.PROVIDER_TIMEOUT


def test_amap_requires_api_key() -> None:
    with pytest.raises(ValueError):
        AmapMapProvider("")


# ── open-meteo ──────────────────────────────────────────────────────────────


@respx.mock
async def test_deepseek_json_mode_sets_response_format() -> None:
    route = respx.post(DEEPSEEK_URL).mock(
        return_value=httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})
    )
    await DeepSeekProvider("sk-test").complete(
        _messages(), tier="fast", max_output_tokens=16, json_mode=True
    )
    assert b'"response_format"' in route.calls[0].request.content


@respx.mock
async def test_deepseek_network_error_is_unavailable() -> None:
    respx.post(DEEPSEEK_URL).mock(side_effect=httpx.ConnectError("dns"))
    with pytest.raises(ProviderError) as excinfo:
        await DeepSeekProvider("sk-test", max_retries=0).complete(
            _messages(), tier="fast", max_output_tokens=16
        )
    assert excinfo.value.code == ErrorCode.PROVIDER_UNAVAILABLE


@respx.mock
async def test_open_meteo_parses_forecast() -> None:
    respx.get(OPEN_METEO_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "daily": {
                    "time": ["2026-09-11"],
                    "precipitation_probability_max": [75],
                    "temperature_2m_max": [30],
                    "temperature_2m_min": [24],
                }
            },
        )
    )
    snapshots = await OpenMeteoWeatherProvider().forecast(LatLng(23.1291, 113.2644))
    assert len(snapshots) == 1
    assert snapshots[0].condition == "rain"
    assert snapshots[0].source == "open_meteo"


@respx.mock
async def test_open_meteo_network_error_is_unavailable() -> None:
    respx.get(OPEN_METEO_URL).mock(side_effect=httpx.ConnectError("dns"))
    with pytest.raises(ProviderError) as excinfo:
        await OpenMeteoWeatherProvider().forecast(LatLng(23.1, 113.2))
    assert excinfo.value.code == ErrorCode.PROVIDER_UNAVAILABLE


@respx.mock
async def test_open_meteo_handles_failures() -> None:
    respx.get(OPEN_METEO_URL).mock(return_value=httpx.Response(200, json={"nope": 1}))
    with pytest.raises(ProviderError) as excinfo:
        await OpenMeteoWeatherProvider().forecast(LatLng(23.1, 113.2))
    assert excinfo.value.code == ErrorCode.PROVIDER_INVALID_RESPONSE

    respx.get(OPEN_METEO_URL).mock(return_value=httpx.Response(502, text="bad gateway"))
    with pytest.raises(ProviderError) as excinfo:
        await OpenMeteoWeatherProvider().forecast(LatLng(23.1, 113.2))
    assert excinfo.value.code == ErrorCode.PROVIDER_UNAVAILABLE

    respx.get(OPEN_METEO_URL).mock(return_value=httpx.Response(200, text="<html>"))
    with pytest.raises(ProviderError) as excinfo:
        await OpenMeteoWeatherProvider().forecast(LatLng(23.1, 113.2))
    assert excinfo.value.code == ErrorCode.PROVIDER_INVALID_RESPONSE

    respx.get(OPEN_METEO_URL).mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(ProviderError) as excinfo:
        await OpenMeteoWeatherProvider().forecast(LatLng(23.1, 113.2))
    assert excinfo.value.code == ErrorCode.PROVIDER_TIMEOUT
