"""Tavily 真实调用冒烟测试：有 Key 才跑，无 Key 自动 skip。

★ 为什么必须有它（与 ``test_llm_live.py`` 同一个理由）★
契约测试用 ``respx`` 拦掉网络，它只能证明"我们正确地处理了**假**响应" ——
而假响应是照着我们**以为**的样子写的。Key 过期、欠费、限流，
或者厂商把 `api_key` 放进 body 这种旧用法关掉，整套测试仍然全绿，
线上第一次真实调用才会暴露。这个文件的职责就是把这类失效放在测试阶段被发现。

★ 刻意只发 2 个请求 ★
Tavily basic search = 1 credit/次，本文件一次运行消耗 2 credits（额度 1000/月）。
第二个请求是 `recency_days`：Tavily 的 `days` 参数**只在 topic=news 时有效**，
而我们目前按 basic 走 —— 这是契约测试照不出来的地方，值得各花 1 credit 钉住。
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from app.core.config import get_pricing_config, get_settings
from app.providers.search.tavily import TavilyProvider
from app.services.cost import PriceBook

pytestmark = pytest.mark.integration

_SKIP_REASON = "未配置 TAVILY_API_KEY（在 .env 里填上即可运行此真实调用测试）"


@pytest.fixture(scope="module")
def provider() -> Iterator[TavilyProvider]:
    """真实 Tavily Provider；无 Key 时跳过（不 mock、不伪造）。"""
    settings = get_settings()
    if not settings.tavily_api_key:
        pytest.skip(_SKIP_REASON)
    yield TavilyProvider(settings.tavily_api_key)


async def test_live_search_returns_parsed_results(provider: TavilyProvider) -> None:
    """最基本的活体检查：能拿到结果，且我们的解析层吃下了真实响应。"""
    health = provider.health()
    assert health.available is True, "配了 Key 却报告不可用，说明装配逻辑坏了"

    results = await provider.search("广州塔 开放时间", max_results=3)

    assert results, "真实搜索返回空 —— 要么查询词有问题，要么响应结构变了"
    first = results[0]
    assert first.url.startswith("http"), f"url 不是可点击的地址：{first.url!r}"
    assert first.title, "标题为空：可能是响应字段改名（我们的解析读 title/content/url/score）"
    assert first.snippet, "摘要为空：同上，字段可能已改名"
    assert first.domain, "domain 没解析出来（urlparse 拿到空 netloc 说明 url 形状变了）"

    # 相关性只做**弱断言**：真实搜索的内容会变，钉死具体结果就是在制造 flaky 测试。
    # 但"一条都不提查询词"确实说明搜错了东西（比如被中间层换成了别的 query）。
    joined = " ".join(f"{r.title} {r.snippet}" for r in results)
    assert "广州" in joined, "结果的标题与摘要里都没有查询词，先怀疑 query 没被正确发出"


async def test_live_search_with_recency_days(provider: TavilyProvider) -> None:
    """`recency_days` 会带出 `days` 参数 —— 它在 basic 检索下是否被接受，只有真调才知道。

    Tavily 文档里 `days` 属于 `topic: news` 的参数。如果它其实会被拒绝，
    我们应当在 Provider 里就不发这个参数，而不是让调用方拿到一个 HTTP 400。
    """
    results = await provider.search("广州 天气", max_results=2, recency_days=7)
    assert isinstance(results, list), "带上 days 参数后调用直接失败，说明该参数在 basic 下不被接受"


def test_live_key_pairs_with_calibrated_pricing() -> None:
    """★ 有 Key ≠ 成本可信 ★

    这是 `test_llm_live.py` 那条教训的另一半：能调通只说明协议对，
    不代表账算得对。这里把"能真实调用的 Provider"与"单价已校准"这两件事
    钉在一起 —— 有人把 `pricing.yaml` 的 tavily 改回 `needs_calibration: true`，
    或者把 `credit_price_usd` 删掉，这条会立刻红。

    刻意**不**发请求：它检查的是配置与记账口径，不是网络。
    """
    settings = get_settings()
    if not settings.tavily_api_key:
        pytest.skip(_SKIP_REASON)

    pricing = get_pricing_config()
    node = pricing.search.get("tavily")
    assert node is not None, "pricing.yaml 里没有 search.tavily —— 真实调用了却没人给它计价"

    price = PriceBook(pricing).search("tavily", "search_basic", units=1)
    assert price.calibrated is True, "Tavily 是已校准单价的 Provider，不该退回未校准"
    assert price.amount_cny > 0, "basic search 记成了 0 元：credits 或单价必然有一处是空的"
