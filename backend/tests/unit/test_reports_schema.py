"""上报 Schema 与纯函数工具的单元测试（PRD FR-11.5 / FR-13.5 / §25）。

★ 这里守的两条 ★
1. **纠错类别是跨文件的一致性**：``feedback`` 表的 CHECK 约束（DDL）、
   ``FeedbackCategory`` 的 Literal 与 ``FEEDBACK_CATEGORIES`` 各写一份是不可避免的，
   但它们一旦漂移，用户就会撞上一个 500（DB 拒绝 INSERT）而不是一条清楚的 422。
   所以用测试把三处钉在一起。
2. **上报进库的内容必须脱敏**：``context`` 是浏览器送来的不可信输入，
   里面完全可能混着被拼进报错信息的 API Key。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import CheckConstraint

from app.api.v1.reports import _require_text, _sanitize_context
from app.core.errors import AppError
from app.core.paths import backend_dir
from app.db.models import ErrorLog, Feedback
from app.schemas.reports import FEEDBACK_CATEGORIES, ErrorReportIn, FeedbackIn

pytestmark = pytest.mark.unit


def _constraint_sql(model: type[object], name: str) -> str:
    for arg in getattr(model, "__table_args__", ()):
        if isinstance(arg, CheckConstraint) and arg.name == name:
            return str(arg.sqltext)
    raise AssertionError(f"没有找到约束 {name}")


# ── 类别三处一致 ────────────────────────────────────────────────────────────


def test_feedback_categories_match_the_check_constraint() -> None:
    sql = _constraint_sql(Feedback, "ck_feedback_category")
    assert tuple(re.findall(r"'([a-z_]+)'", sql)) == FEEDBACK_CATEGORIES


def test_feedback_categories_match_the_literal_type() -> None:
    """``FeedbackIn`` 的校验必须与那份元组同源（否则会有一条永远收不到的类别）。"""
    from typing import get_args

    assert get_args(FeedbackIn.model_fields["category"].annotation) == FEEDBACK_CATEGORIES


def test_feedback_categories_are_in_the_migration_too() -> None:
    """迁移里的 CHECK 约束是真正的 DDL —— 它与模型漂移时，测试库与开发库会不一致。"""
    migration = next(
        path
        for path in (Path(backend_dir()) / "alembic" / "versions").glob("*.py")
        if "ck_feedback_category" in path.read_text(encoding="utf-8")
    )
    text = migration.read_text(encoding="utf-8")
    start = text.index("op.create_table('feedback'")
    block = text[start : text.index("op.create_index('ix_feedback_created'", start)]
    assert tuple(re.findall(r"'([a-z_]+)'", block.split("ck_feedback_category")[0].split("sa.CheckConstraint(")[-1])) == (
        FEEDBACK_CATEGORIES
    )


def test_error_levels_match_the_check_constraint() -> None:
    sql = _constraint_sql(ErrorLog, "ck_error_level")
    assert tuple(re.findall(r"'([a-z]+)'", sql)) == (
        "debug",
        "info",
        "warning",
        "error",
        "critical",
    )


# ── 入参校验 ────────────────────────────────────────────────────────────────


def test_unknown_category_and_extra_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        FeedbackIn(category="wrong_name")
    with pytest.raises(ValidationError):
        # 多余的字段必须被拒（模型是 extra=forbid），而这个"多传一个参数"的写法
        # mypy 本来就看不惯 —— 正是要测这种越界调用。
        FeedbackIn(category="other", severity="high")  # type: ignore[call-arg]


def test_error_report_requires_component_and_message() -> None:
    with pytest.raises(ValidationError):
        ErrorReportIn(component="", message="boom")
    with pytest.raises(ValidationError):
        ErrorReportIn(component="trip-page")  # type: ignore[call-arg]
    report = ErrorReportIn(component="trip-page", message="boom")
    # 默认是 error 而不是 info：一条\"不知道严重程度\"的上报按最需要看的那种处理
    assert report.level == "error"


# ── 纯函数：文本上限与脱敏 ──────────────────────────────────────────────────


def test_require_text_treats_blank_as_absent() -> None:
    assert _require_text(None, field_name="x", max_chars=10) is None
    assert _require_text("   ", field_name="x", max_chars=10) is None
    assert _require_text("  有用的话  ", field_name="x", max_chars=10) == "有用的话"


def test_require_text_rejects_overlong_instead_of_truncating() -> None:
    with pytest.raises(AppError) as excinfo:
        _require_text("很" * 11, field_name="反馈内容", max_chars=10)
    assert excinfo.value.code == "INVALID_INPUT"
    # 说清\"多长、上限多少\"：只说\"太长\"会让用户反复试
    assert excinfo.value.context["max_chars"] == 10


def test_sanitize_context_drops_junk_and_scrubs_secrets() -> None:
    # 键刻意写成 `Any`：非字符串键（数字）正是要验证被丢掉的那一类，
    # 直接写明 dict 元素类型，免得 mypy 把"这个用例在测什么"当成类型错误。
    payload: dict[Any, Any] = {
        "path": "/trip/abc",
        "api_key": "sk-1234567890abcdefgh",
        1: "数字键不保留",
        "huge": "x" * 500,
    }
    out = _sanitize_context(payload)
    assert out is not None
    assert out["path"] == "/trip/abc"
    assert "sk-" not in out["api_key"]
    assert "1" not in out
    assert len(out["huge"]) <= 200 + 20  # 截断后带一段\"(+N chars)\"的说明


def test_sanitize_context_returns_none_when_there_is_nothing_to_store() -> None:
    assert _sanitize_context(None) is None
    assert _sanitize_context({}) is None


def test_sanitize_context_serializes_nested_values() -> None:
    """嵌套结构不保留、但也不丢：压成一个字符串，否则\"context 有东西\"会消失。"""
    out = _sanitize_context({"info": {"a": 1}})
    assert out == {"info": '{"a": 1}'}
