"""成本后台的集成测试（PRD FR-12 AC-12.2）。

★ 这组测试的三条重点 ★

1. **访问控制真的挡住人**：配了 ``ADMIN_TOKEN`` 时限流之外的任何请求都拿不到账单；
   生产环境里这个端点也存在（与只在开发环境注册的 ``/dev`` 面板不同），
   所以它必须自己守门。
2. **单次规划成本按 ``request_id`` 聚合**，不是 ``cost_logs`` 的行平均 ——
   后者会随"我们加了多少种外部调用"漂移，而它服务的是一条对外的红线（≤ ¥0.5）。
3. **没有 ``request_id`` 的记录被单独计数**，不进平均值：把它们当成零成本的规划
   会系统性拉低数字，那种"达标"毫无价值。
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, insert, select
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import clear_config_cache, get_limits_config, get_settings
from app.db.models import CostLog

pytestmark = pytest.mark.integration

_URL = get_settings().database_url


@pytest.fixture
def cost_rows() -> Iterator[Any]:
    """插入"今天的" ``cost_logs`` 行（已提交），用完删干净。

    必须提交：这个端点是**跨会话**地读整张表，写在未提交的事务里它根本看不见。
    """
    created: list[int] = []

    def create(rows: list[dict[str, Any]]) -> list[int]:
        ids: list[int] = []

        async def run() -> None:
            engine = create_async_engine(_URL)
            try:
                async with engine.begin() as conn:
                    for row in rows:
                        ids.append(
                            int(
                                (
                                    await conn.execute(
                                        insert(CostLog)
                                        .values(
                                            category=row.get("category", "llm"),
                                            provider=row.get("provider", "deepseek"),
                                            operation=row.get("operation", "plan_narrative"),
                                            units=1,
                                            unit_price=Decimal("0"),
                                            amount_cny=Decimal(str(row["amount_cny"])),
                                            cache_hit=False,
                                            pricing_calibrated=row.get("calibrated", True),
                                            request_id=row.get("request_id"),
                                        )
                                        .returning(CostLog.id)
                                    )
                                ).scalar_one()
                            )
                        )
            finally:
                await engine.dispose()

        asyncio.run(run())
        created.extend(ids)
        return ids

    try:
        yield create
    finally:
        if created:

            async def cleanup() -> None:
                engine = create_async_engine(_URL)
                try:
                    async with engine.begin() as conn:
                        await conn.execute(delete(CostLog).where(CostLog.id.in_(created)))
                finally:
                    await engine.dispose()

            asyncio.run(cleanup())


@pytest.fixture
def isolated_cost_logs() -> Iterator[None]:
    """把 ``cost_logs`` 隔离成"只有本用例插入的行"：快照 → 清空 → 原样恢复。

    ★ 为什么需要它 ★ 下面的分位数测试断言的是**整表**的 p50 / max，而测试库是
    **跨运行持久**的：上一次没跑完就中断的规划测试、或手工冒烟留下的行都会留在
    表里（实测：3 笔 0 元 + 1 笔 0.0576 元的历史规划把 p50 拉成了 0）。那不是
    被测代码错了，是测试假设了"表里只有我插的行"。所以不是放宽断言，而是给
    测试一个确定性的表状态，测完恢复原样（别人的数据一行不少、连 id 都不变）。
    """
    snapshot: list[dict[str, Any]] = []

    async def drain() -> None:
        engine = create_async_engine(_URL)
        try:
            async with engine.begin() as conn:
                rows = (await conn.execute(select(CostLog))).mappings().all()
                snapshot.extend(dict(row) for row in rows)
                await conn.execute(delete(CostLog))
        finally:
            await engine.dispose()

    asyncio.run(drain())
    try:
        yield
    finally:

        async def restore() -> None:
            engine = create_async_engine(_URL)
            try:
                async with engine.begin() as conn:
                    await conn.execute(delete(CostLog))
                    if snapshot:
                        # 显式带回 id 与 created_at：id 是 BIGSERIAL（允许显式插入），
                        # created_at 有 server_default，不回填会变成"恢复时刻"。
                        await conn.execute(insert(CostLog), snapshot)
            finally:
                await engine.dispose()

        asyncio.run(restore())


def _summary(client: TestClient, **params: Any) -> Any:
    return client.get("/api/v1/admin/cost/summary", params=params)


def test_summary_is_reachable_from_localhost_without_a_token(client: TestClient) -> None:
    """本机开发者不该为了看一次成本先造一个 Token（与 ``/dev`` 同一套口径）。"""
    response = _summary(client)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    data = body["data"]
    assert data["window_days"] == 7
    assert data["plans"]["plans"] >= 0
    # 没有样本时 p50 必须是 None 而不是 0：0 会被读成"每次规划的成本为零"，
    # 而"还没有数据"与"数据是零"是两件事。
    p50 = data["plans"]["p50_cny"]
    assert p50 is None or p50 >= 0
    assert set(data) >= {
        "by_category",
        "by_provider",
        "plans",
        "cache",
        "pricing_calibrated",
        "daily",
        "breakers",
    }


def test_breakers_come_from_limits_yaml(client: TestClient) -> None:
    """★ 与 /health 同一条教训（TASKS 问题 73）★

    阈值必须报**真正生效的那个**（``config/limits.yaml``），
    而不是环境变量里的镜像值 —— 否则页面上的数字与拦截点会不一致。
    """
    limits = get_limits_config().cost.circuit_breaker
    data = _summary(client).json()["data"]
    assert data["breakers"] == {
        "plan_total_cny": limits.plan_total_cny,
        "plan_search_cny": limits.plan_search_cny,
        "plan_map_calls": limits.plan_map_calls,
        "plan_llm_calls": limits.plan_llm_calls,
    }


def test_token_when_configured_is_required(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ADMIN_TOKEN", "s3cret-token")
    clear_config_cache()
    try:
        missing = _summary(client)
        assert missing.status_code == 401, missing.text
        assert missing.json()["error"]["code"] == "ADMIN_REQUIRED"

        wrong = client.get(
            "/api/v1/admin/cost/summary", headers={"X-Admin-Token": "nope"}
        )
        assert wrong.status_code == 401

        ok = client.get(
            "/api/v1/admin/cost/summary", headers={"X-Admin-Token": "s3cret-token"}
        )
        assert ok.status_code == 200, ok.text
    finally:
        monkeypatch.delenv("ADMIN_TOKEN", raising=False)
        clear_config_cache()


def test_plan_cost_stats_group_by_request_not_by_row(
    client: TestClient, isolated_cost_logs: None, cost_rows: Any
) -> None:
    """一次规划花 0.30（三行）与一次规划花 0.06（一行）**不是**同一件事。

    行平均会算出 0.12，按 request_id 聚合才是 {0.30, 0.06} —— 平均 0.18、最大 0.30。
    p50 / max 是对整表断言的绝对值，所以先隔离出干净的表（见 isolated_cost_logs）。
    """
    before = _summary(client, days=1).json()["data"]["plans"]
    cost_rows(
        [
            {"amount_cny": "0.10", "request_id": "11111111-1111-1111-1111-111111111111"},
            {"amount_cny": "0.10", "request_id": "11111111-1111-1111-1111-111111111111"},
            {"amount_cny": "0.10", "request_id": "11111111-1111-1111-1111-111111111111"},
            {"amount_cny": "0.06", "request_id": "22222222-2222-2222-2222-222222222222"},
        ]
    )
    after = _summary(client, days=1).json()["data"]["plans"]

    assert after["plans"] == before["plans"] + 2  # 四行 → 两笔
    assert after["max_cny"] == pytest.approx(0.30)
    assert after["p50_cny"] in {0.06, 0.30}  # 最近秩：永远是真实存在的那一笔
    assert any(item["calls"] == 3 for item in after["top"])


def test_rows_without_request_id_are_counted_but_kept_out_of_the_average(
    client: TestClient, cost_rows: Any
) -> None:
    before = _summary(client, days=1).json()["data"]["plans"]
    cost_rows([{"amount_cny": "0.01", "request_id": None}])
    after = _summary(client, days=1).json()["data"]["plans"]

    assert after["rows_without_request"] == before["rows_without_request"] + 1
    assert after["plans"] == before["plans"]  # 不进平均值，也不假装成\"一次规划\"


def test_within_target_is_null_when_any_plan_is_uncalibrated(
    client: TestClient, cost_rows: Any
) -> None:
    """金额本身不可信时，"低于红线"是一句没有根据的话 —— 报 null 而不是 true。"""
    cost_rows(
        [
            {
                "amount_cny": "0.01",
                "request_id": "33333333-3333-3333-3333-333333333333",
                "calibrated": False,
            }
        ]
    )
    plans = _summary(client, days=1).json()["data"]["plans"]
    assert plans["uncalibrated_plans"] >= 1
    assert plans["within_target"] is None
    # 门槛由配置给出，页面不自己写死一个数字
    assert plans["target_cny"] == get_limits_config().cost.warn_plan_cny
