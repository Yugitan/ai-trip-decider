"""数据库引擎与会话。

- 引擎按 URL 缓存，便于测试用不同库（tripdecider_test）。
- 全异步（asyncpg）。注意：本项目**不定义 ORM relationship()**，避免异步惰性加载报错；
  所有关联查询都用显式 select + join。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings

_engines: dict[str, AsyncEngine] = {}
_sessionmakers: dict[str, async_sessionmaker[AsyncSession]] = {}


def _engine_kwargs() -> dict[str, Any]:
    settings = get_settings()
    return {
        "echo": False,
        "pool_pre_ping": True,
        "pool_size": 5,
        "max_overflow": 10,
        "pool_recycle": 1800,
        # 慢查询保护：连接层超时，避免请求被数据库拖死
        "connect_args": {"timeout": 10, "server_settings": {"application_name": settings.env}},
    }


def get_engine(url: str | None = None) -> AsyncEngine:
    settings = get_settings()
    target = url or settings.database_url
    if target not in _engines:
        _engines[target] = create_async_engine(target, **_engine_kwargs())
    return _engines[target]


def get_sessionmaker(url: str | None = None) -> async_sessionmaker[AsyncSession]:
    target = url or get_settings().database_url
    if target not in _sessionmakers:
        _sessionmakers[target] = async_sessionmaker(
            bind=get_engine(target),
            expire_on_commit=False,
            autoflush=False,
        )
    return _sessionmakers[target]


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖：每请求一个会话，异常时回滚。"""
    async with get_sessionmaker()() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def dispose_engines() -> None:
    """应用关闭时释放连接池。"""
    for engine in list(_engines.values()):
        await engine.dispose()
    _engines.clear()
    _sessionmakers.clear()
