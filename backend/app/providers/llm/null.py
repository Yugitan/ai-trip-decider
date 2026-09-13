"""LLM 降级实现：未配置任何 LLM Key 时使用。

它**不假装成功**：``complete()`` 直接抛 ``PROVIDER_UNAVAILABLE``，
由上层（意图解析、叙事生成）降级到规则引擎与模板文案。
"""

from __future__ import annotations

from collections.abc import Sequence

from app.core.errors import ErrorCode, ProviderError
from app.providers.base import ProviderHealth
from app.providers.llm.base import LlmMessage, LlmResponse, LlmTier

__all__ = ["NullLlmProvider"]

_REASON = "未配置 LLM API Key（规则引擎 + 模板文案兜底）"


class NullLlmProvider:
    """永远不可用的 LLM Provider。存在的意义是让"缺少 LLM"成为**一种正常状态**。"""

    name = "disabled"

    def model_for(self, tier: LlmTier) -> str:
        return f"disabled:{tier}"

    def health(self) -> ProviderHealth:
        return ProviderHealth(name=self.name, available=False, detail=_REASON, degraded=True)

    async def complete(
        self,
        messages: Sequence[LlmMessage],
        *,
        tier: LlmTier,
        max_output_tokens: int,
        temperature: float = 0.2,
        json_mode: bool = False,
    ) -> LlmResponse:
        raise ProviderError(
            self.name,
            "complete",
            kind=ErrorCode.PROVIDER_UNAVAILABLE,
            message=_REASON,
        )
