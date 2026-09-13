"""日志与脱敏的单元测试。

这条最容易被忽视但风险最高：日志一旦泄露 API Key 或用户自由文本全文，
就是安全事故（PRD §24）。
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest

from app.core.logging import ContextLogger, JsonFormatter, get_logger, scrub, set_request_id

pytestmark = pytest.mark.unit


def _format(record: logging.LogRecord) -> dict[str, Any]:
    parsed: dict[str, Any] = json.loads(JsonFormatter().format(record))
    return parsed


def _make_record(**extra: object) -> logging.LogRecord:
    record = logging.LogRecord(
        name="tripdecider.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello",
        args=(),
        exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_scrub_redacts_api_keys() -> None:
    text = "调用失败，key=sk-abcdef1234567890abcdef 请检查"
    out = scrub(text)
    assert "sk-abcdef1234567890abcdef" not in out
    assert "[REDACTED]" in out


@pytest.mark.parametrize(
    "raw",
    [
        "api_key=AKIAIOSFODNN7EXAMPLE",
        "apiKey: SUPERSECRETVALUE123",
        "API-KEY = anothersecretvalue",
        "Authorization: Bearer abcdefghijklmnop",
        "token=qqqqqqqqqqqqqqqq",
        "access_token: zzzzzzzzzzzzzzzz",
        "password=hunter2hunter2",
        "secret: mysecretvalue123",
    ],
)
def test_scrub_redacts_common_secret_shapes(raw: str) -> None:
    """回归：`Authorization: Bearer <token>` 的分隔符是空格，早期实现漏掉了它。"""
    scrubbed = scrub(raw)
    # 取出原文里的秘密部分（分隔符后的最后一个词）并断言它已消失
    secret = raw.split()[-1].split("=")[-1].split(":")[-1].strip()
    assert secret not in scrubbed, f"{raw!r} 的秘密未被脱敏：{scrubbed!r}"
    assert "REDACTED" in scrubbed


def test_scrub_does_not_over_redact_normal_chinese_text() -> None:
    """不能把正常的中文行程描述误伤成 REDACTED。"""
    text = "第一次来广州，不想走太多路，晚上想看夜景，预算 300 元"
    assert scrub(text) == text


def test_scrub_truncates_long_text() -> None:
    out = scrub("啊" * 5000, max_len=100)
    assert len(out) < 200
    assert "+4900 chars" in out


def test_scrub_keeps_short_safe_text() -> None:
    assert scrub("广州一日游") == "广州一日游"


def test_json_formatter_includes_event_and_context() -> None:
    payload = _format(_make_record(event="plan.started", context={"stage": 1}))
    assert payload["event"] == "plan.started"
    assert payload["context"] == {"stage": 1}
    assert payload["level"] == "info"


def test_context_logger_merges_extra_instead_of_overwriting() -> None:
    """回归测试：标准库 LoggerAdapter 会丢掉调用方传入的 extra（结构化日志静默失效）。"""
    logger = get_logger("unit-test")
    assert isinstance(logger, ContextLogger)
    captured: dict[str, object] = {}

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured["component"] = getattr(record, "component", None)
            captured["event"] = getattr(record, "event", None)
            captured["context"] = getattr(record, "context", None)

    handler = _Capture()
    logger.logger.addHandler(handler)
    logger.logger.setLevel(logging.INFO)
    try:
        logger.info("测试", extra={"event": "unit.event", "context": {"k": "v"}})
    finally:
        logger.logger.removeHandler(handler)

    assert captured["component"] == "unit-test", "适配器自带的 component 必须保留"
    assert captured["event"] == "unit.event", "调用方传入的 event 必须保留"
    assert captured["context"] == {"k": "v"}, "调用方传入的 context 必须保留"


def test_request_id_is_attached_and_cleared() -> None:
    set_request_id("abc123")
    try:
        assert _format(_make_record())["request_id"] == "abc123"
    finally:
        set_request_id(None)
    assert "request_id" not in _format(_make_record())


def test_context_strings_are_scrubbed() -> None:
    payload = _format(_make_record(context={"key": "sk-abcdef1234567890abcdef"}))
    assert "sk-abcdef1234567890abcdef" not in json.dumps(payload, ensure_ascii=False)


def test_formatter_serializes_non_ascii_without_escaping() -> None:
    payload = _format(_make_record())
    assert payload["message"] == "hello"
    assert json.dumps(payload, ensure_ascii=False).find("\\u") == -1
