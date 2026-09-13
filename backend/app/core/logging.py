"""结构化日志（JSON 行）。

脱敏要求（PRD §24）：
- 不记录 API Key、完整 IP、用户自由文本全文。
- request_id 贯穿前后端，便于排查。
"""

from __future__ import annotations

import json
import logging
import re
import sys
import time
import uuid
from contextvars import ContextVar
from typing import Any

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)

_SECRET_PATTERNS = (
    # 常见厂商 Key 前缀
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    # Authorization: Bearer <token>（注意分隔符是空格，不是 = 或 :）
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{8,}"),
    # key=value / key: value 形式（含 api_key / access_token / secret / password 等）
    re.compile(
        r"(?i)\b(api[_-]?key|apikey|access[_-]?token|auth[_-]?token|token|secret|password|passwd)\b"
        r"\s*[=:]\s*[\"']?[^\s,;\"']+"
    ),
)


def new_request_id() -> str:
    return uuid.uuid4().hex


def set_request_id(value: str | None) -> None:
    request_id_var.set(value)


def get_request_id() -> str | None:
    return request_id_var.get()


def scrub(value: str, *, max_len: int = 500) -> str:
    """脱敏 + 截断。任何要写进日志的用户输入都必须先过这里。"""
    out = value
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub("[REDACTED]", out)
    if len(out) > max_len:
        out = out[:max_len] + f"…(+{len(value) - max_len} chars)"
    return out


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created))
            + f".{int(record.msecs):03d}",
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": scrub(record.getMessage()),
        }
        rid = get_request_id()
        if rid:
            payload["request_id"] = rid
        for key in ("component", "event", "code", "latency_ms", "provider", "operation"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        context = getattr(record, "context", None)
        if isinstance(context, dict):
            payload["context"] = {k: (scrub(v) if isinstance(v, str) else v) for k, v in context.items()}
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


class ContextLogger(logging.LoggerAdapter[logging.Logger]):
    """把调用方传入的 ``extra`` 与适配器自带的 ``extra`` **合并**。

    为什么不用标准库的 ``logging.LoggerAdapter``：在 Python 3.13 之前，它的
    ``process()`` 会直接 ``kwargs["extra"] = self.extra``，把调用方传的
    ``event`` / ``context`` 全部丢掉 —— 结构化日志会静默退化成纯文本。
    这里显式实现 ``process`` 以保证两边的字段都保留。
    """

    def process(self, msg: Any, kwargs: Any) -> tuple[Any, Any]:
        merged: dict[str, Any] = dict(self.extra or {})
        merged.update(kwargs.get("extra") or {})
        kwargs["extra"] = merged
        return msg, kwargs


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())
    # 降低第三方噪声
    for noisy in ("uvicorn.access", "httpx", "httpcore", "asyncio"):
        logging.getLogger(noisy).setLevel("WARNING")


def get_logger(component: str) -> ContextLogger:
    return ContextLogger(logging.getLogger(f"tripdecider.{component}"), {"component": component})
