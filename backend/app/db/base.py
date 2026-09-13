"""SQLAlchemy 声明式基类与列类型辅助。

设计取舍：
1. **不定义 relationship()**。本项目全异步（asyncpg），惰性加载会抛 MissingGreenlet，
   因此统一使用显式 ``select()`` + 显式 join，把"取哪些数据"写在查询里而不是隐式触发。
2. **分值用 float，金额用 Decimal**。分值是启发式排序量（Numeric(4,3) asdecimal=False），
   金额涉及计费必须精确（Numeric(10,2) → Decimal），两者语义不同，刻意不统一。
3. 枚举用 text + CHECK 约束，而不是 PG 原生 enum —— 迁移更简单，加值不需要 ALTER TYPE。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import DateTime, Numeric, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。"""


# ── 可复用的列构造器（每次调用返回新实例，绝不复用同一个 mapped_column）──────


def uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )


def uuid_fk(
    target: str,
    *,
    ondelete: str = "CASCADE",
    nullable: bool = False,
    index: bool = False,
) -> Mapped[uuid.UUID]:
    from sqlalchemy import ForeignKey

    return mapped_column(
        UUID(as_uuid=True),
        ForeignKey(target, ondelete=ondelete),
        nullable=nullable,
        index=index,
    )


def created_at_col() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


def updated_at_col() -> Mapped[datetime]:
    return mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


def ts(nullable: bool = True) -> Mapped[datetime | None]:
    return mapped_column(DateTime(timezone=True), nullable=nullable)


def score() -> Mapped[float | None]:
    """0..1 的启发式分值。NULL 表示未评估，**禁止用 0.5 占位**（PRD §8.7 R5）。"""
    return mapped_column(Numeric(4, 3, asdecimal=False), nullable=True)


def money() -> Mapped[Decimal | None]:
    """金额（CNY）。"""
    return mapped_column(Numeric(10, 2), nullable=True)


def amount6() -> Mapped[Decimal]:
    """高精度金额（成本日志用，单位元，保留 6 位小数）。"""
    return mapped_column(Numeric(10, 6), nullable=False, server_default=text("0"))


def text_array() -> Mapped[list[str]]:
    from sqlalchemy import ARRAY, Text

    return mapped_column(ARRAY(Text), nullable=False, server_default=text("'{}'::text[]"))


def jsonb(nullable: bool = True) -> Mapped[dict[str, Any] | None]:
    from sqlalchemy.dialects.postgresql import JSONB

    return mapped_column(JSONB, nullable=nullable)


def jsonb_required() -> Mapped[dict[str, Any]]:
    """NOT NULL 的 JSONB 列。与 jsonb() 分开是为了让 mypy 能区分可空性。"""
    from sqlalchemy.dialects.postgresql import JSONB

    return mapped_column(JSONB, nullable=False)


def bigint_pk() -> Mapped[int]:
    from sqlalchemy import BigInteger

    return mapped_column(BigInteger, primary_key=True, autoincrement=True)
