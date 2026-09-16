"""数据库引擎与会话。

- 引擎按 URL 缓存，便于测试用不同库（tripdecider_test）。
- 全异步（asyncpg）。注意：本项目**不定义 ORM relationship()**，避免异步惰性加载报错；
  所有关联查询都用显式 select + join。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.core.faults import DB_SLOW_DELAY_S, active_fault

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
    """FastAPI 依赖：每请求一个会话，异常时回滚。

    ★ 网络/数据库故障注入就装在这一道门上（PRD §23.5 的 ``db_down`` / ``db_slow``）★
    两个都刻意**不是** AppError：

    - ``db_down`` 抛真的 :class:`OperationalError`。数据库挂掉在现实里就是这种异常，
      如果注入一个我们自己的异常类型，测到的就只是"我们自己写的那条分支"。
      它必须被翻译成 503 ``DB_UNAVAILABLE`` + request_id（``main._db_error``），
      而不是一个含糊的 500 —— 用户与运维都需要知道"是库不可用"。
    - ``db_slow`` 睡 3 秒（PRD 写的时长），用来验证慢请求告警（``http.slow``）
      与 ``X-Elapsed-Ms`` 真的在工作：没有这条注入，"卡了 3 秒但没人知道"
      只能等到线上才发现。
    """
    fault = active_fault()
    if fault == "db_down":
        raise OperationalError(
            "SELECT 1", {}, Exception("FAULT_INJECTION=db_down：模拟数据库不可用")
        )
    if fault == "db_slow":
        await asyncio.sleep(DB_SLOW_DELAY_S)
    async with get_sessionmaker()() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def dispose_engines() -> None:
    """应用关闭时释放连接池。

    ★ 先把缓存摘掉，再尝试关闭 ★
    引擎是按 URL 缓存的，而连接绑在**创建它的事件循环**上。换到一个新循环里关闭一个
    旧循环建的引擎时，asyncpg 会抛 ``got Future ... attached to a different loop``
    （关闭要 await 回它自己的循环）。旧实现把 ``clear()`` 放在循环之后，于是这一抛会
    留下一个**已经不可用但仍在缓存里**的引擎：一次无害的关闭失败变成后面每一条用例
    的失败（实测是间歇性的 —— 取决于那一刻池里有没有空闲连接）。
    现在缓存先清：最坏情况只是一个连接池没有被优雅关闭，进程退出时由操作系统回收。
    """
    engines = list(_engines.values())
    _engines.clear()
    _sessionmakers.clear()
    for engine in engines:
        try:
            await engine.dispose()
        except RuntimeError:
            # 只吞"循环不对"这一类（跨循环关闭）；其余照旧抛，别顺手把真问题藏了。
            continue
