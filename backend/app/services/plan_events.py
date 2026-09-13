"""SSE 事件总线：把后台规划线程的进度送到 ``GET /trips/{id}/stream``（PRD §7.3）。

为什么是**进程内**的：
    规划本身跑在同一进程的后台任务里（参见 ``api/v1/trips.py``）。为此引入 Redis
    只会让"本地跑一次规划"多一个必须启动的组件，而 M4 的并发假设是单实例
    （PRD §15 的缓存也是进程内 LRU + Postgres）。

★ 三个必须处理的现实情况 ★
1. **客户端后到**：POST 返回 202 时后台任务可能已经跑完。因此每个 channel 保留
   一段事件缓冲，订阅者先读到缓冲再读实时流 —— 否则前端会卡在"生成中"。
2. **客户端早退**：订阅者断开时要把队列从 channel 里摘掉，否则内存随请求数泄漏。
3. **没人订阅**：channel 也要有 TTL，否则每个请求的缓冲都会永久留在内存里。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "PlanChannel",
    "PlanEventBus",
    "get_event_bus",
    "reset_event_bus",
]

#: 每个 channel 最多保留多少个事件（再多也没有客户端会重放）
_MAX_BUFFER = 64
#: channel 多久没动静就回收（秒）。规划本身远短于这个时间，只用来兜底防泄漏。
_CHANNEL_TTL_S = 15 * 60
#: 同时存在的 channel 上限，超出时淘汰最旧的
_MAX_CHANNELS = 512

PlanEvent = tuple[str, dict[str, Any]]


@dataclass
class PlanChannel:
    """一次规划请求的事件通道。"""

    buffer: list[PlanEvent] = field(default_factory=list)
    subscribers: set[asyncio.Queue[PlanEvent | None]] = field(default_factory=set)
    closed: bool = False
    updated_at: float = field(default_factory=time.monotonic)

    def subscribe(self) -> tuple[asyncio.Queue[PlanEvent | None], list[PlanEvent]]:
        """订阅并拿到当前缓冲的快照。

        订阅与取快照之间**没有 await**，因此在事件循环里是原子的：
        不会出现"快照里没有、队列里也没有"的事件空洞。
        """
        queue: asyncio.Queue[PlanEvent | None] = asyncio.Queue()
        self.subscribers.add(queue)
        return queue, list(self.buffer)

    def unsubscribe(self, queue: asyncio.Queue[PlanEvent | None]) -> None:
        self.subscribers.discard(queue)

    def publish(self, event: PlanEvent) -> None:
        self.buffer.append(event)
        if len(self.buffer) > _MAX_BUFFER:
            del self.buffer[: len(self.buffer) - _MAX_BUFFER]
        self.updated_at = time.monotonic()
        for queue in list(self.subscribers):
            queue.put_nowait(event)

    def close(self) -> None:
        """发送终止信号：``None`` 让每个订阅者的循环自然结束。"""
        self.closed = True
        self.updated_at = time.monotonic()
        for queue in list(self.subscribers):
            queue.put_nowait(None)


@dataclass
class PlanEventBus:
    channels: dict[str, PlanChannel] = field(default_factory=dict)

    def channel(self, request_id: str) -> PlanChannel:
        self._purge()
        channel = self.channels.get(request_id)
        if channel is None:
            channel = PlanChannel()
            self.channels[request_id] = channel
            # 插入后再收敛一次：只在插入前清理会让通道数稳定地停在"上限 + 1"。
            self._purge()
        return channel

    def existing(self, request_id: str) -> PlanChannel | None:
        """已存在的通道（不新建）。订阅方需要区分"还没开始"与"已被回收"。"""
        return self.channels.get(request_id)

    def publish(self, request_id: str, event: str, data: Mapping[str, Any]) -> None:
        self.channel(request_id).publish((event, dict(data)))

    def close(self, request_id: str) -> None:
        self.channel(request_id).close()

    def _purge(self) -> None:
        now = time.monotonic()
        expired = [
            key
            for key, channel in self.channels.items()
            if channel.closed and not channel.subscribers and now - channel.updated_at > _CHANNEL_TTL_S
        ]
        for key in expired:
            del self.channels[key]
        while len(self.channels) > _MAX_CHANNELS:
            oldest = sorted(self.channels.items(), key=lambda kv: kv[1].updated_at)
            removed = False
            for key, channel in oldest:
                if not channel.subscribers:
                    del self.channels[key]
                    removed = True
                    break
            if not removed:
                # 所有通道都被订阅着：保留它们（正在读的客户端不能被打断）。
                # 这是刻意选择 —— 宁可短暂超出上限，也不要让用户的事件流莫名中断。
                break


_bus: PlanEventBus | None = None


def get_event_bus() -> PlanEventBus:
    global _bus
    if _bus is None:
        _bus = PlanEventBus()
    return _bus


def reset_event_bus() -> None:
    """测试用：清空总线，避免用例之间互相看到对方的事件。"""
    global _bus
    _bus = PlanEventBus()
