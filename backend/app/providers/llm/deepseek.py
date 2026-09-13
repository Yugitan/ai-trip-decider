"""DeepSeek LLM Provider（OpenAI 兼容的 ``/chat/completions``）。

为什么选 DeepSeek（PRD §2424 Q1）：中文旅游场景够用且成本最低。
接口与 OpenAI 兼容，因此切换到 OpenAI/Anthropic 时只需新增一个 Provider，
业务层代码零改动。

★ 成本诚实性 ★
DeepSeek 的价格页是 JS 渲染的，静态抓取拿不到 —— ``config/pricing.yaml`` 里
其单价仍是 ``null + needs_calibration: true``。**这不影响本 Provider 工作**：
调用照常发生、token 数照常记录，只是成本报表会如实标注"未校准"。
M3 首次真实调用后必须回填单价（见 pricing.yaml 顶部校准流程）。

★ 生命周期 ★
每次调用创建一个 ``httpx.AsyncClient``。MVP 的 LLM 调用量极低（每规划 0–2 次），
换来的是"不需要管理连接池生命周期"的简单性；日后再换成共享 client 即可。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import httpx

from app.core.errors import ErrorCode, ProviderError
from app.providers.base import ProviderHealth
from app.providers.llm.base import LlmMessage, LlmResponse, LlmTier, LlmUsage

__all__ = ["DeepSeekProvider"]

_DEFAULT_MODELS: dict[str, str] = {"fast": "deepseek-chat", "strong": "deepseek-reasoner"}


class DeepSeekProvider:
    name = "deepseek"

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.deepseek.com",
        models: Mapping[str, str] | None = None,
        timeout_s: float = 20.0,
        max_retries: int = 1,
    ) -> None:
        if not api_key:
            raise ValueError("DeepSeekProvider 需要非空 api_key（无 Key 请使用 NullLlmProvider）")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._models: dict[str, str] = dict(models) if models else dict(_DEFAULT_MODELS)
        self._timeout_s = timeout_s
        self._max_retries = max(0, max_retries)

    def model_for(self, tier: LlmTier) -> str:
        return self._models.get(tier, _DEFAULT_MODELS[tier])

    def health(self) -> ProviderHealth:
        return ProviderHealth(name=self.name, available=True, detail="已配置 API Key")

    async def complete(
        self,
        messages: Sequence[LlmMessage],
        *,
        tier: LlmTier,
        max_output_tokens: int,
        temperature: float = 0.2,
        json_mode: bool = False,
    ) -> LlmResponse:
        model = self.model_for(tier)
        payload: dict[str, object] = {
            "model": model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "max_tokens": max_output_tokens,
            "temperature": temperature,
        }
        if json_mode:
            # DeepSeek 要求 prompt 里出现 "json" 字样才会真正开启 JSON 输出；
            # 这里只声明 response_format，prompt 由调用方负责约定（见 app/prompts/）。
            payload["response_format"] = {"type": "json_object"}
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        response = await self._post("/chat/completions", payload, headers)
        return _parse_completion(response, provider=self.name, model=model)

    async def _post(
        self, path: str, payload: dict[str, object], headers: Mapping[str, str]
    ) -> httpx.Response:
        attempts = self._max_retries + 1
        last_exc: httpx.HTTPError | None = None
        for _ in range(attempts):
            try:
                async with httpx.AsyncClient(timeout=self._timeout_s) as client:
                    response = await client.post(
                        f"{self._base_url}{path}", json=payload, headers=dict(headers)
                    )
            except httpx.TimeoutException as exc:
                last_exc = exc
                continue
            except httpx.HTTPError as exc:
                last_exc = exc
                continue
            if response.status_code >= 500:
                # 5xx 视为可重试；4xx 是请求本身的问题，重试没有意义
                last_exc = httpx.HTTPStatusError(
                    f"HTTP {response.status_code}", request=response.request, response=response
                )
                continue
            if response.status_code >= 400:
                raise ProviderError(
                    self.name,
                    "complete",
                    kind=ErrorCode.PROVIDER_UNAVAILABLE,
                    message=f"{self.name} 返回 HTTP {response.status_code}（请求被拒绝，重试无意义）",
                )
            return response

        kind = (
            ErrorCode.PROVIDER_TIMEOUT
            if isinstance(last_exc, httpx.TimeoutException)
            else ErrorCode.PROVIDER_UNAVAILABLE
        )
        raise ProviderError(
            self.name,
            "complete",
            kind=kind,
            message=f"{self.name} 调用失败（已重试 {attempts} 次）：{type(last_exc).__name__}",
        )


def _parse_completion(response: httpx.Response, *, provider: str, model: str) -> LlmResponse:
    """把 chat completions 响应翻译成 ``LlmResponse``。

    任何结构不符（缺 choices / content 为空 / 非 JSON）都归为
    ``PROVIDER_INVALID_RESPONSE`` —— 它和"网络失败"是不同的故障，
    上层可能想对前者做 schema 重试、对后者直接降级。

    ★ ``usage`` 缺失时不能当成 0 token ★
    有些 OpenAI 兼容网关（代理、自建中转）会省略 ``usage``。此时 token 数不是
    "0"而是"未知"：直接填 0 会让成本报表得到一个 ``calibrated=True`` 的 0 元记录，
    等于向用户声称"这次没花钱"。所以这里置 ``usage_known=False``，由成本层
    记成未校准（同 `pricing.yaml` 里单价为 null 的处理）。
    """
    try:
        body = response.json()
        content = body["choices"][0]["message"]["content"]
        if not isinstance(content, str) or not content.strip():
            raise ValueError("content 为空")
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise ProviderError(
            provider,
            "complete",
            kind=ErrorCode.PROVIDER_INVALID_RESPONSE,
            message=f"{provider} 返回了无法解析的响应结构：{exc}",
        ) from exc

    raw_usage = body.get("usage")
    usage_known = isinstance(raw_usage, Mapping)
    usage: Mapping[str, object] = raw_usage if usage_known else {}
    return LlmResponse(
        text=content,
        model=str(body.get("model", model)),
        provider=provider,
        usage=LlmUsage(
            tokens_in=_as_int(usage.get("prompt_tokens")),
            tokens_out=_as_int(usage.get("completion_tokens")),
            tokens_cached=_as_int(usage.get("prompt_cache_hit_tokens")),
            usage_known=usage_known,
        ),
    )


def _as_int(value: object) -> int:
    """token 计数；脏数据（字符串 / None / 小数）一律回退到 0。

    ``usage`` 是外部输入，``int(usage.get(...))`` 遇到 ``"12"`` 能用但遇到
    ``None``/``"n/a"`` 会抛 TypeError/ValueError，把一次成功的调用变成异常。
    """
    if isinstance(value, bool) or value is None:
        return 0
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, float):
        return max(0, int(value))
    if isinstance(value, str):
        try:
            return max(0, int(value.strip()))
        except ValueError:
            return 0
    return 0
