"""LLM 使用情况的可观测性（响应 ``meta.llm`` + 降级说明 + 成本落库）。

★ 为什么单独守这个 ★
"我们接了 LLM"如果只写在文档与 commit message 里，就只是一句无从核验的声明。
这个文件把三处事实钉在一起，任何人改坏其中一处都会红：

1. **响应**：``meta.llm`` 必须如实写明是否配置、是否真的调用、模型、token、
   每个任务走了模型还是规则、以及降级原因；
2. **降级说明**：没配 Key 时必须出现在 ``degraded_modes`` 里，
   不允许出现"悄悄降级、界面看上去一切正常"；
3. **成本落库**：没有真实调用就不该在 ``cost_logs`` 里留下 LLM 记录，
   ``trip.total_cost_cny`` 也不该凭空冒出一个数字。

测试环境由 ``conftest`` 强制把 LLM 压成 ``NullLlmProvider``（见那里的说明），
所以这里测的是**无 Key 路径**；真实 Key 的活体检查在 ``test_llm_live.py``。
"""

from __future__ import annotations

import asyncio
import itertools
import uuid
from typing import cast

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.config import get_settings
from app.db.models import CostLog, RateLimitCounter, Trip
from app.services.llm_planner import TASK_INTENT, TASK_NARRATIVE

pytestmark = pytest.mark.integration


def _clear_rate_limits() -> None:
    """限流计数器是**状态**而不是业务数据。

    不清空的话本文件的第 2 个用例就会被自己上一次请求的配额挡住（每 IP 每天 5 次
    冷规划），失败信息看起来像业务错误、实际是测试相互污染。
    """
    url = get_settings().database_url

    async def run() -> None:
        engine = create_async_engine(url)
        try:
            async with engine.begin() as conn:
                await conn.execute(delete(RateLimitCounter))
        finally:
            await engine.dispose()

    asyncio.run(run())


@pytest.fixture(autouse=True)
def _fresh_rate_limits() -> None:
    _clear_rate_limits()

_counter = itertools.count(7000)


def _payload() -> dict[str, object]:
    """每次用不同的预算，避免命中 plan_cache（缓存复用的路径另有用例覆盖）。"""
    return {
        "city": "guangzhou",
        "days": 1,
        "people": 2,
        "preferences": ["food"],
        "pace": "relaxed",
        "budget": {"amount": 300 + next(_counter), "scope": "per_person"},
        "free_text": "想去老城区逛逛",
    }


def _session(value: object) -> AsyncSession:
    return cast(AsyncSession, value)


def test_plan_response_reports_llm_status_honestly(client: TestClient) -> None:
    response = client.post("/api/v1/trips:plan?sync=true", json=_payload())
    assert response.status_code == 200, response.text
    body = response.json()

    llm = body["meta"]["llm"]
    assert llm is not None, "规划响应必须带 LLM 使用状态"
    assert set(llm) == {
        "enabled",
        "used",
        "provider",
        "model",
        "prompt_version",
        "calls",
        "cache_hits",
        "tokens_in",
        "tokens_out",
        "cost_cny",
        "cost_calibrated",
        "tasks",
        "fallback_reasons",
    }
    # 测试环境没有可用的 LLM：必须如实说“没用到”，而不是留空或假装用过
    assert llm["enabled"] is False
    assert llm["used"] is False
    assert llm["calls"] == 0
    assert llm["cost_cny"] == "0"
    # 两个任务分别走到哪一层，也要写清楚
    assert llm["tasks"][TASK_INTENT] == "rule"
    assert llm["tasks"][TASK_NARRATIVE] == "rule"
    # “没配 Key” 不等于 “降级失败”：前者由 degraded_modes 与 enabled 表达
    assert llm["fallback_reasons"] == []
    assert llm["prompt_version"], "prompt 版本必须能被读到（缓存失效靠它）"


def test_missing_llm_key_shows_up_in_degraded_modes(client: TestClient) -> None:
    response = client.post("/api/v1/trips:plan?sync=true", json=_payload())
    assert response.status_code == 200, response.text
    degraded = response.json()["meta"]["degraded_modes"]
    assert any(entry.startswith("llm:") for entry in degraded), (
        f"未配置 LLM 时必须出现在 degraded_modes 里，实际：{degraded}"
    )


async def test_no_llm_call_means_no_cost_rows_and_zero_total(
    client: TestClient, db_session: object
) -> None:
    response = client.post("/api/v1/trips:plan?sync=true", json=_payload())
    assert response.status_code == 200, response.text
    body = response.json()
    # 按 trip_id 查，而不是 meta.request_id：后者是**请求级**中间件 id，
    # 与 trip_requests.id 不是同一个东西 —— 用它查 cost_logs 会永远得到空列表，
    # 让这个断言变成“测试通过但什么都没验证”（第一版就是这么写的）。
    trip_id = uuid.UUID(body["data"]["trip_id"])

    session = _session(db_session)
    cost_rows = (
        (await session.execute(select(CostLog).where(CostLog.trip_id == trip_id))).scalars().all()
    )
    assert [row.category for row in cost_rows] == [], (
        "没有真实外部调用，就不该在 cost_logs 里留下记录"
    )

    trip = (await session.execute(select(Trip).where(Trip.id == trip_id))).scalar_one()
    assert trip.total_cost_cny == 0, "没有外部调用时行程成本必须是 0，不能凭空冒出一个数"
