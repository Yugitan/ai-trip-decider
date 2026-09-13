"""DeepSeek 真实调用冒烟测试：有 Key 才跑，无 Key 自动 skip。

★ 为什么必须有它 ★
契约测试用 ``respx`` 拦掉网络，它只能证明"我们正确地处理了**假**响应"。
一旦 Key 过期、欠费、被限流或 base_url 改错，整套测试仍然全绿 ——
线上第一次真实调用才会暴露问题。这个文件的唯一职责就是让**Key 失效在测试阶段被发现**。

★ 跳过是刻意的，但不会假装跑过 ★
PRD §15.5 要求"无 Key 也必须能跑"，所以没有 Key 时 skip（并把原因写清楚）。
要跑它：在 ``.env`` 里配上 ``DEEPSEEK_API_KEY`` 即可，无需其它开关
（它直接构造 Provider，不依赖应用层被降级成的 NullLlmProvider）。

成本：本文件一次运行只发 2 个极短请求（max_tokens 很小），用于验证 Key 与协议。
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator

import pytest

from app.core.config import get_settings
from app.providers.base import ProviderHealth
from app.providers.llm.base import LlmMessage
from app.providers.llm.deepseek import DeepSeekProvider

pytestmark = pytest.mark.integration

_SKIP_REASON = "未配置 DEEPSEEK_API_KEY（在 .env 里填上即可运行此真实调用测试）"


@pytest.fixture(scope="module")
def provider() -> Iterator[DeepSeekProvider]:
    """真实 DeepSeek Provider；无 Key 时跳过（不 mock、不伪造）。"""
    settings = get_settings()
    if not settings.deepseek_api_key:
        pytest.skip(_SKIP_REASON)
    yield DeepSeekProvider(
        settings.deepseek_api_key,
        base_url=settings.llm_base_url,
        models={
            "fast": settings.llm_tier_fast_model,
            "strong": settings.llm_tier_strong_model,
        },
        timeout_s=settings.llm_timeout_s,
        max_retries=settings.llm_max_retries,
    )


async def test_live_completion_returns_text_usage_and_health(
    provider: DeepSeekProvider,
) -> None:
    """最基本的活体检查：能拿到非空文本、能拿到 token 数、health 说可用。"""
    health: ProviderHealth = provider.health()
    assert health.available is True, "配了 Key 却报告不可用，说明装配逻辑坏了"

    started = time.perf_counter()
    response = await provider.complete(
        [LlmMessage(role="user", content="只回复两个字：你好")],
        tier="fast",
        max_output_tokens=16,
        temperature=0.0,
    )
    elapsed_ms = int((time.perf_counter() - started) * 1000)

    assert response.text.strip(), "模型返回了空文本"
    assert response.provider == "deepseek"
    assert response.model, "响应里没有模型名，无法归因到具体模型"
    assert elapsed_ms < int(get_settings().llm_timeout_s * 1000) + 2000, (
        "耗时接近超时阈值：Key 可用但链路异常慢，值得排查"
    )
    # ★ token 数必须是**已知**的 ★
    # 缺失时 usage_known=False 是诚实做法（成本层据此标未校准），
    # 但真实 DeepSeek 接口应该返回 usage；这里断言"真实接口确实给了"。
    assert response.usage.usage_known is True, (
        "DeepSeek 未返回 usage：成本报表会标成未校准，请确认接口是否变更"
    )
    assert response.usage.tokens_in > 0


async def test_live_json_mode_returns_parseable_object(provider: DeepSeekProvider) -> None:
    """json_mode 真的能拿到 JSON 对象（规划链路把模型输出喂给 schema 校验，这是前提）。"""
    response = await provider.complete(
        [
            LlmMessage(
                role="system",
                content="只输出 JSON 对象，不要输出解释或 Markdown。",
            ),
            LlmMessage(
                role="user",
                content='请输出 {"ok": true} 这样的 JSON 对象，键名固定为 ok。',
            ),
        ],
        tier="fast",
        max_output_tokens=32,
        temperature=0.0,
        json_mode=True,
    )
    parsed = json.loads(response.text)
    assert isinstance(parsed, dict), "json_mode 下返回的不是 JSON 对象"
