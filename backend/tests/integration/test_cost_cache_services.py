"""缓存存储 / 成本落库 / 限流器的集成测试（真实测试库）。

为什么必须有这一层：这三者的 bug 都**只在真实数据库上才暴露** ——
``ON CONFLICT`` 语义、``timestamptz`` 的时区处理、``hit_count`` 的自增、
``cost_logs`` 的聚合口径，用 mock 测等于没测。

测试跑在 ``db_session``（不提交）里，因此不会污染测试库。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import cast

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import City, CostLog, LlmCache, PlanCache, SearchCache, Trip, TripRequest
from app.services.cache import CacheStore
from app.services.cost import CostEntry, CostStore
from app.services.rate_limit import RateLimiter

pytestmark = pytest.mark.integration


def _session(db_session: object) -> AsyncSession:
    return cast(AsyncSession, db_session)


async def _make_trip(session: AsyncSession) -> uuid.UUID:
    """plan_cache.trip_id 对 trips 有外键，所以得先造一条真实 trip（含 request 与 city）。"""
    city_id = (await session.execute(select(City.id).limit(1))).scalar_one()
    request = TripRequest(
        session_id=uuid.uuid4(), raw_input={"test": True}, params_hash=f"test-{uuid.uuid4().hex}"
    )
    session.add(request)
    await session.flush()
    trip = Trip(
        request_id=request.id,
        session_id=request.session_id,
        city_id=city_id,
        intent_snapshot={},
        route_count=1,
    )
    session.add(trip)
    await session.flush()
    return trip.id


# ── 限流 ────────────────────────────────────────────────────────────────────


async def test_rate_limiter_allows_up_to_limit_then_blocks(db_session: object) -> None:
    limiter = RateLimiter(_session(db_session))
    key = f"test:rl:{uuid.uuid4().hex}"
    first = await limiter.hit(key, limit=2, window_s=3600)
    second = await limiter.hit(key, limit=2, window_s=3600)
    third = await limiter.hit(key, limit=2, window_s=3600)
    assert (first.allowed, first.used) == (True, 1)
    assert (second.allowed, second.used) == (True, 2)
    assert third.allowed is False
    assert third.used == 3
    assert third.remaining == 0
    assert 0 < third.retry_after_s <= 3600


async def test_rate_limiter_zero_limit_means_unlimited(db_session: object) -> None:
    """配置里写 0 表示"关闭该限制"，不该把它当成"一次都不许"。"""
    limiter = RateLimiter(_session(db_session))
    result = await limiter.hit(f"test:rl:{uuid.uuid4().hex}", limit=0, window_s=60)
    assert result.allowed is True
    assert result.used == 0


# ── 缓存存储 ────────────────────────────────────────────────────────────────


async def test_search_cache_roundtrip_and_hit_count(db_session: object) -> None:
    session = _session(db_session)
    store = CacheStore(session)
    key = f"search:{uuid.uuid4().hex}"
    await store.put_search(
        cache_key=key,
        provider="tavily",
        query="广州 美食",
        result={"results": [{"title": "x"}]},
        ttl_hours=168,
    )
    assert await store.get_search(key) == {"results": [{"title": "x"}]}

    row = (
        await session.execute(SearchCache.__table__.select().where(SearchCache.cache_key == key))
    ).mappings().one()
    assert row["hit_count"] == 1
    assert row["last_hit_at"] is not None
    assert await store.get_search("missing-key") is None


async def test_search_cache_ignores_expired_rows(db_session: object) -> None:
    session = _session(db_session)
    store = CacheStore(session)
    key = f"search:{uuid.uuid4().hex}"
    session.add(
        SearchCache(
            cache_key=key,
            provider="tavily",
            query="过期",
            result={"results": []},
            expires_at=datetime.now(UTC) - timedelta(seconds=1),
        )
    )
    await session.flush()
    assert await store.get_search(key) is None, "过期缓存必须当作未命中，而不是返回旧数据"


async def test_llm_cache_roundtrip_and_never_expiring(db_session: object) -> None:
    session = _session(db_session)
    store = CacheStore(session)
    key = f"llm:{uuid.uuid4().hex}"
    await store.put_llm(
        cache_key=key,
        tier="fast",
        model="deepseek-chat",
        prompt_hash="abc",
        task="intent_parse",
        response={"answer": "ok"},
        ttl_hours=None,  # 结构化输出：靠 prompt 版本失效
        tokens_in=10,
        tokens_out=5,
    )
    assert await store.get_llm(key) == {"answer": "ok"}
    assert await store.get_llm("missing-key") is None

    row = (
        await session.execute(LlmCache.__table__.select().where(LlmCache.cache_key == key))
    ).mappings().one()
    assert row["tokens_in"] == 10
    assert row["expires_at"] > datetime.now(UTC) + timedelta(days=3000)


async def test_llm_cache_ignores_expired_rows(db_session: object) -> None:
    session = _session(db_session)
    store = CacheStore(session)
    key = f"llm:{uuid.uuid4().hex}"
    session.add(
        LlmCache(
            cache_key=key,
            tier="fast",
            model="m",
            prompt_hash="h",
            task="t",
            response={"x": 1},
            expires_at=datetime.now(UTC) - timedelta(seconds=1),
        )
    )
    await session.flush()
    assert await store.get_llm(key) is None


async def test_plan_cache_roundtrip_and_expiry(db_session: object) -> None:
    session = _session(db_session)
    store = CacheStore(session)
    trip_id = await _make_trip(session)
    params = uuid.uuid4().hex
    await store.put_plan(
        params_hash_value=params, kb_version="gz-test", trip_id=trip_id, ttl_hours=168
    )
    assert await store.get_plan(params, "gz-test") == trip_id
    # kb_version 变了必须未命中（知识库更新后旧方案自动失效）
    assert await store.get_plan(params, "gz-other") is None

    expired_params = uuid.uuid4().hex
    session.add(
        PlanCache(
            params_hash=expired_params,
            kb_version="gz-test",
            trip_id=trip_id,
            expires_at=datetime.now(UTC) - timedelta(seconds=1),
        )
    )
    await session.flush()
    assert await store.get_plan(expired_params, "gz-test") is None


async def test_plan_cache_hit_count_increments(db_session: object) -> None:
    session = _session(db_session)
    store = CacheStore(session)
    params = uuid.uuid4().hex
    trip_id = await _make_trip(session)
    await store.put_plan(params_hash_value=params, kb_version="gz", trip_id=trip_id, ttl_hours=1)
    await store.get_plan(params, "gz")
    await store.get_plan(params, "gz")
    row = (
        await session.execute(PlanCache.__table__.select().where(PlanCache.params_hash == params))
    ).mappings().one()
    assert row["hit_count"] == 2


# ── 成本落库与聚合 ──────────────────────────────────────────────────────────


async def test_cost_store_persists_and_summarizes(db_session: object) -> None:
    session = _session(db_session)
    store = CostStore(session)
    request_id = uuid.uuid4()
    entries = [
        CostEntry(
            category="search",
            provider="tavily",
            amount_cny=Decimal("0.057600"),
            calibrated=True,
            units=1,
        ),
        CostEntry(
            category="llm",
            provider="deepseek",
            amount_cny=Decimal("0"),
            calibrated=False,
            model="deepseek-chat",
            units=0,
        ),
    ]
    written = await store.persist(entries, request_id=request_id)
    assert written == 2

    summary = await store.summary(days=1)
    categories = {row["category"]: row for row in summary["by_category"]}
    assert categories["search"]["calls"] >= 1
    assert Decimal(categories["search"]["amount_cny"]) >= Decimal("0.057600")
    assert summary["pricing_calibrated"] is False, "含未校准记录时报表必须承认成本不可信"
    assert "未校准" in summary["note"]

    by_provider = await store.by_provider(days=1)
    assert any(row["provider"] == "deepseek" for row in by_provider)


async def test_cost_store_persist_empty_returns_zero(db_session: object) -> None:
    assert await CostStore(_session(db_session)).persist([]) == 0


async def test_cost_summary_uncalibrated_count_respects_window(db_session: object) -> None:
    """未校准记录数必须与按类别的聚合用同一个时间窗。

    早期实现里分组聚合带 ``created_at >= since``、而未校准计数是老的全量统计，
    于是表头写着「近 7 天」、那个数字却是历史上所有未校准行的总数 —— 报表的全部
    价值就在于「这两个数能被相信」，一个不带窗口的计数能直接把它毁掉。
    """
    session = _session(db_session)
    store = CostStore(session)

    before = await store.summary(days=1)
    old = CostLog(
        category="llm",
        provider="deepseek",
        amount_cny=Decimal("0"),
        pricing_calibrated=False,
        units=0,
        created_at=datetime.now(UTC) - timedelta(days=30),
    )
    session.add(old)
    await session.flush()

    recent = await store.summary(days=1)
    assert recent["uncalibrated_rows"] == before["uncalibrated_rows"], (
        "30 天前的记录不该被算进「近 1 天」的未校准计数"
    )

    wider = await store.summary(days=90)
    assert wider["uncalibrated_rows"] == before["uncalibrated_rows"] + 1, (
        "把窗口放宽到 90 天之后，它必须重新出现"
    )
    assert wider["uncalibrated_rows"] > recent["uncalibrated_rows"]
