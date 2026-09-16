"""数据库引擎缓存的生命周期（`app/db/session.py`）。

为什么单独立一个文件：这里守的不是 SQL，而是**引擎缓存与事件循环的关系** ——
它是这个仓库里反复造成"假绿 / 间歇性失败"的地方（M1 的 `/health` 抛
`Event loop is closed`、集成测试里跨事件循环复用引擎），所以它的行为值得被钉住。

本文件不需要数据库：用的是替身引擎对象，测的是缓存与异常处理这两条分支。
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db import session as db_session

pytestmark = pytest.mark.unit

_URL = "postgresql+asyncpg://unit-test/whatever"


class _StubEngine:
    """只实现 `dispose` 的引擎替身。"""

    def __init__(self, *, raises: BaseException | None = None) -> None:
        self.raises = raises
        self.disposed = 0

    async def dispose(self) -> None:
        self.disposed += 1
        if self.raises is not None:
            raise self.raises


async def test_dispose_engines_clears_the_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = _StubEngine()
    monkeypatch.setitem(db_session._engines, _URL, cast("AsyncEngine", engine))
    monkeypatch.setitem(db_session._sessionmakers, _URL, cast("Any", object()))

    await db_session.dispose_engines()

    assert engine.disposed == 1, "池子必须被关闭"
    assert _URL not in db_session._engines
    assert _URL not in db_session._sessionmakers


async def test_cache_is_cleared_even_when_a_cross_loop_close_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """关闭一个"别的循环建的"引擎会抛 RuntimeError，缓存**也必须**被清掉。

    旧实现的顺序是"先关、后清"，于是这一抛会留下一个不可用但仍在缓存里的引擎，
    后面每条用例都会拿到它并再抛一次 —— 一次无害的失败变成一串失败
    （实测是间歇性的：取决于那一刻池里有没有空闲连接）。
    """
    exploding = _StubEngine(raises=RuntimeError("got Future attached to a different loop"))
    healthy = _StubEngine()
    monkeypatch.setitem(db_session._engines, _URL, cast("AsyncEngine", exploding))
    monkeypatch.setitem(db_session._engines, f"{_URL}-2", cast("AsyncEngine", healthy))
    monkeypatch.setitem(db_session._sessionmakers, _URL, cast("Any", object()))

    await db_session.dispose_engines()

    assert not db_session._engines, "缓存必须被清空，不能留下不可用的引擎"
    assert not db_session._sessionmakers
    assert healthy.disposed == 1, "一个引擎关闭失败不该让其余引擎漏掉关闭"


async def test_other_errors_are_not_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """只容忍"循环不对"这一类。其余异常照旧抛 —— 吞掉真问题比不清理更坏。"""
    engine = _StubEngine(raises=ValueError("这不是循环问题"))
    monkeypatch.setitem(db_session._engines, _URL, cast("AsyncEngine", engine))

    with pytest.raises(ValueError):
        await db_session.dispose_engines()

    assert not db_session._engines, "即使抛出，缓存也已经摘掉了（否则会留下毒丸）"
