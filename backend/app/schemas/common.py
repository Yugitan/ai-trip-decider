"""统一响应包与通用 Schema。

约定（PRD §7.2）：
成功 → {"ok": true, "data": {...}, "meta": {...}}
失败 → {"ok": false, "error": {...}, "meta": {...}}
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ApiMeta(BaseModel):
    """每个响应都带元信息：可缓存性、降级状态、耗时、成本。"""

    model_config = ConfigDict(extra="forbid")

    request_id: str | None = None
    cached: bool = False
    cache_layer: str | None = None
    degraded_modes: list[str] = Field(default_factory=list)
    elapsed_ms: int | None = None
    #: 本次请求里 LLM 的**真实**使用情况（是否配置、是否真的调用、模型、
    #: token、金额与是否已校准、每个任务走了模型还是规则、降级原因）。
    #: 没有它，"我们用了大模型"就只是一句无从核验的声明。
    llm: dict[str, Any] | None = None


class ErrorBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    hint: str = ""
    context: dict[str, Any] = Field(default_factory=dict)


def ok_envelope(data: Any, meta: ApiMeta | None = None) -> dict[str, Any]:
    return {
        "ok": True,
        "data": data,
        "error": None,
        "meta": (meta or ApiMeta()).model_dump(),
    }


def error_envelope(body: ErrorBody, meta: ApiMeta | None = None) -> dict[str, Any]:
    return {
        "ok": False,
        "data": None,
        "error": body.model_dump(),
        "meta": (meta or ApiMeta()).model_dump(),
    }
