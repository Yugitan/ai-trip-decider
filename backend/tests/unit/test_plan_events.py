"""SSE 事件总线单元测试（PRD §7.3）。

守三件事：
1. **客户端后到也能拿到完整事件**：POST 返回 202 时后台可能已经跑完，
   订阅者必须能从缓冲里重放 —— 否则前端永远停在"生成中"。
2. **终止信号一定送达**：订阅循环靠 ``None`` 结束，漏发就会挂着不返回。
3. **通道会被回收**：没有 TTL/上限，每个请求的缓冲都会永久留在内存里。
"""

from __future__ import annotations

import asyncio
import time

import pytest

from app.services.plan_events import (
    PlanChannel,
    PlanEventBus,
    get_event_bus,
    reset_event_bus,
)

pytestmark = pytest.mark.unit


def test_publish_then_subscribe_replays_buffer() -> None:
    """后到的订阅者要能读到已经发生的事件（缓冲重放）。"""
    bus = PlanEventBus()
    bus.publish("req", "plan.started", {"stages": 4})
    bus.publish("req", "plan.progress", {"stage": 1, "pct": 15})

    channel = bus.channel("req")
    queue, backlog = channel.subscribe()
    assert [event for event, _ in backlog] == ["plan.started", "plan.progress"]
    # 历史事件只走缓冲、不进队列，否则订阅者会把同一条进度读两遍
    assert queue.qsize() == 0


def test_live_subscriber_receives_events() -> None:
    bus = PlanEventBus()
    channel = bus.channel("req")
    queue, backlog = channel.subscribe()
    assert backlog == []

    bus.publish("req", "plan.completed", {"trip_id": "t1"})
    event = queue.get_nowait()
    assert event == ("plan.completed", {"trip_id": "t1"})


def test_close_delivers_terminator_to_all_subscribers() -> None:
    bus = PlanEventBus()
    channel = bus.channel("req")
    first, _ = channel.subscribe()
    second, _ = channel.subscribe()
    bus.close("req")
    assert first.get_nowait() is None
    assert second.get_nowait() is None
    assert channel.closed is True


def test_unsubscribe_stops_delivery() -> None:
    """客户端断开时必须摘掉队列，否则事件会往一个没人读的队列里堆。"""
    bus = PlanEventBus()
    channel = bus.channel("req")
    queue, _ = channel.subscribe()
    channel.unsubscribe(queue)
    bus.publish("req", "plan.progress", {"pct": 40})
    assert queue.qsize() == 0


def test_buffer_is_capped() -> None:
    channel = PlanChannel()
    for index in range(200):
        channel.publish(("plan.progress", {"pct": index}))
    assert len(channel.buffer) <= 64, "缓冲必须封顶，否则长任务会一直占内存"
    assert channel.buffer[-1] == ("plan.progress", {"pct": 199})


def test_existing_does_not_create_channel() -> None:
    bus = PlanEventBus()
    assert bus.existing("unknown") is None
    assert "unknown" not in bus.channels


def test_purge_removes_expired_closed_channels() -> None:
    bus = PlanEventBus()
    channel = bus.channel("old")
    channel.close()
    channel.updated_at = time.monotonic() - 3600
    bus.channel("new")  # 触发 purge
    assert "old" not in bus.channels
    assert "new" in bus.channels


def test_purge_keeps_channel_with_subscribers() -> None:
    """还有订阅者的通道不能被回收：否则正在读的客户端会突然拿不到后续事件。"""
    bus = PlanEventBus()
    channel = bus.channel("busy")
    channel.subscribe()
    channel.close()
    channel.updated_at = time.monotonic() - 3600
    bus.channel("other")
    assert "busy" in bus.channels


def test_channel_cap_evicts_oldest_unsubscribed() -> None:
    bus = PlanEventBus()
    for index in range(520):
        channel = bus.channel(f"req-{index}")
        channel.close()
        channel.updated_at = time.monotonic() - (520 - index)
    assert len(bus.channels) <= 512


def test_async_consumption_pattern() -> None:
    """模拟真实的 SSE 消费循环：先重放，再等终止信号。"""

    async def consume() -> list[str]:
        bus = PlanEventBus()
        bus.publish("req", "plan.started", {})
        channel = bus.channel("req")
        queue, backlog = channel.subscribe()

        async def producer() -> None:
            await asyncio.sleep(0)
            bus.publish("req", "plan.completed", {"trip_id": "t"})
            bus.close("req")

        task = asyncio.create_task(producer())
        seen = [event for event, _ in backlog]
        while True:
            item = await queue.get()
            if item is None:
                break
            seen.append(item[0])
        await task
        return seen

    assert asyncio.run(consume()) == ["plan.started", "plan.completed"]


def test_singleton_and_reset() -> None:
    reset_event_bus()
    first = get_event_bus()
    assert get_event_bus() is first
    first.publish("req", "plan.started", {})
    reset_event_bus()
    assert get_event_bus() is not first
