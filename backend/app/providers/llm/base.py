"""LLM Provider 契约（PRD §16.1 分层模型）。

分档的意义（成本红线）：Tier1 fast 做分类/抽取/解析，Tier2 strong 只做排序/叙事。
**能用规则就不用 AI，能用 fast 就不用 strong** —— 这条原则在上层编排里体现，
Provider 只负责按 tier 选对模型。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ValidationError

from app.core.errors import ErrorCode, ProviderError
from app.providers.base import ProviderHealth

__all__ = [
    "LlmMessage",
    "LlmProvider",
    "LlmResponse",
    "LlmTier",
    "LlmUsage",
    "Role",
    "complete_json",
]

LlmTier = Literal["fast", "strong"]
Role = Literal["system", "user", "assistant"]


@dataclass(frozen=True, slots=True)
class LlmMessage:
    role: Role
    content: str


@dataclass(frozen=True, slots=True)
class LlmUsage:
    tokens_in: int = 0
    tokens_out: int = 0
    tokens_cached: int = 0
    # 有些厂商不返回 token 数。此时**不能写 0 假装"没花钱"**，
    # 而应把 usage_known=False 传下去，让成本报表如实标注"用量未知"。
    usage_known: bool = True


@dataclass(frozen=True, slots=True)
class LlmResponse:
    text: str
    model: str
    provider: str
    usage: LlmUsage = field(default_factory=LlmUsage)


@runtime_checkable
class LlmProvider(Protocol):
    """LLM 契约。实现方负责超时、重试与把 HTTP 错误翻译成 ``ProviderError``。"""

    name: str

    def model_for(self, tier: LlmTier) -> str: ...

    async def complete(
        self,
        messages: Sequence[LlmMessage],
        *,
        tier: LlmTier,
        max_output_tokens: int,
        temperature: float = 0.2,
        json_mode: bool = False,
    ) -> LlmResponse: ...

    def health(self) -> ProviderHealth: ...


async def complete_json[TModel: BaseModel](
    provider: LlmProvider,
    schema: type[TModel],
    messages: Sequence[LlmMessage],
    *,
    tier: LlmTier,
    max_output_tokens: int,
    temperature: float = 0.0,
    retries: int = 1,
) -> TModel:
    """要求模型输出 JSON 并**强校验**成 ``schema``；失败重试 ``retries`` 次后抛错。

    为什么必须强校验（PRD FR-02 AC-2.5、§16.2）：LLM 的输出是不可信输入。
    直接把 ``json.loads`` 的结果喂给业务代码，等于让模型决定我们的数据结构。
    校验失败**不返回半个结果**，而是抛 ``PROVIDER_INVALID_RESPONSE``，
    由上层降级到规则引擎 —— 这样"模型抽风"永远不会变成"用户看到乱码"。
    """
    last_error: Exception | None = None
    attempts = retries + 1
    for _ in range(attempts):
        response = await provider.complete(
            messages,
            tier=tier,
            max_output_tokens=max_output_tokens,
            temperature=temperature,
            json_mode=True,
        )
        try:
            return schema.model_validate(_parse_json_object(response.text))
        except (ValidationError, ValueError) as exc:
            last_error = exc
    raise ProviderError(
        provider.name,
        "complete_json",
        kind=ErrorCode.PROVIDER_INVALID_RESPONSE,
        message=(
            f"{provider.name} 连续 {attempts} 次返回不符合 schema 的 JSON：{last_error}"
        ),
    )


def _parse_json_object(text: str) -> object:
    """从模型输出里取出 JSON 对象。

    允许模型用 ```json 围栏包裹（很常见），但**只接受对象**：
    数组或裸标量说明它没理解任务，属于无效响应而不是"格式略有出入"。
    """
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("```", 2)[1]
        if stripped.startswith("json"):
            stripped = stripped[4:]
        stripped = stripped.strip()
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise ValueError(f"不是合法 JSON：{exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"期望 JSON 对象，实际是 {type(parsed).__name__}")
    return parsed
