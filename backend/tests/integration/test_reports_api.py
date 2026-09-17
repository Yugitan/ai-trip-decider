"""上报端点的集成测试（PRD §7.2 · FR-11.5 · FR-13.5）。

为什么这两个端点值得单独一组测试：它们是**唯二**由浏览器直接写入数据库的入口。
因此这里关心的不是"能不能写进去"，而是：

- 写进去的内容**是不是用户以为的那个**（空白留言不落库、超长直接拒绝而不是悄悄截断）；
- 引用不存在的 trip/place 时**报的是那条引用的问题**（404 语义码），
  而不是撞外键后变成一个"数据库不可用"的 500；
- 送进来的上下文**必须脱敏**（浏览器里的报错信息可能正好包含一把 Key）；
- 限流真的生效，并且提示里有确定的恢复时间。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_limits_config, get_settings
from app.db.models import ErrorLog, Feedback, RateLimitCounter

pytestmark = pytest.mark.integration

_URL = get_settings().database_url

#: 本模块写进库的行。测试结束时删干净 —— 上报是状态，不是业务数据，
#: 留着会让开发库的 feedback 表慢慢积累一批"测试写的话"。
_CREATED_FEEDBACK: list[uuid.UUID] = []
_CREATED_ERRORS: list[int] = []


async def _with_session(work: Any) -> Any:
    engine = create_async_engine(_URL)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            result = await work(session)
            await session.commit()
            return result
    finally:
        await engine.dispose()


def _execute(callback: Any) -> None:
    asyncio.run(_with_session(callback))


@pytest.fixture(autouse=True)
def _fresh_state() -> Iterator[None]:
    """限流计数与本次写入都清干净。"""
    _CREATED_FEEDBACK.clear()
    _CREATED_ERRORS.clear()
    _execute(lambda session: session.execute(delete(RateLimitCounter)))

    yield

    async def cleanup(session: Any) -> None:
        await session.execute(delete(RateLimitCounter))
        if _CREATED_FEEDBACK:
            await session.execute(delete(Feedback).where(Feedback.id.in_(list(_CREATED_FEEDBACK))))
        if _CREATED_ERRORS:
            await session.execute(delete(ErrorLog).where(ErrorLog.id.in_(list(_CREATED_ERRORS))))

    _execute(cleanup)


def _post_feedback(client: TestClient, payload: dict[str, Any]) -> Any:
    response = client.post("/api/v1/feedback", json=payload)
    if response.status_code == 200:
        _CREATED_FEEDBACK.append(uuid.UUID(response.json()["data"]["id"]))
    return response


def _post_error(client: TestClient, payload: dict[str, Any]) -> Any:
    response = client.post("/api/v1/errors", json=payload)
    if response.status_code == 200:
        _CREATED_ERRORS.append(int(response.json()["data"]["id"]))
    return response


def _feedback_row(feedback_id: str) -> dict[str, Any] | None:
    async def fetch(session: Any) -> dict[str, Any] | None:
        row = (
            await session.execute(select(Feedback).where(Feedback.id == uuid.UUID(feedback_id)))
        ).scalar_one_or_none()
        if row is None:
            return None
        return {
            "status": row.status,
            "message": row.message,
            "place_id": None if row.place_id is None else str(row.place_id),
        }

    result: dict[str, Any] | None = asyncio.run(_with_session(fetch))
    return result


def _error_row(row_id: int) -> dict[str, Any] | None:
    async def fetch(session: Any) -> dict[str, Any] | None:
        row = (
            await session.execute(select(ErrorLog).where(ErrorLog.id == row_id))
        ).scalar_one_or_none()
        if row is None:
            return None
        return {
            "level": row.level,
            "component": row.component,
            "message": row.message,
            "context": row.context,
        }

    result: dict[str, Any] | None = asyncio.run(_with_session(fetch))
    return result


def _first_place_id(client: TestClient) -> str:
    items = client.get("/api/v1/cities/guangzhou/places?limit=1").json()["data"]["items"]
    assert items, "知识库为空，这组测试没有意义"
    return str(items[0]["id"])


# ── POST /feedback ──────────────────────────────────────────────────────────


def test_feedback_needs_only_a_category(client: TestClient) -> None:
    """只勾一个类别、不写留言也必须能提交 —— 强制留言会让用户直接离开。"""
    response = _post_feedback(client, {"category": "wrong_hours"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    data = body["data"]
    assert data["id"] and data["status"] == "open"
    # 回执必须说明我们会拿它做什么，而不是一句"感谢反馈"
    assert "人工复核" in data["note"]

    row = _feedback_row(data["id"])
    assert row is not None
    assert row["status"] == "open"
    assert row["message"] is None


def test_feedback_links_a_real_place(client: TestClient) -> None:
    place_id = _first_place_id(client)
    response = _post_feedback(
        client,
        {"category": "wrong_price", "message": " 这份价格是错的 ", "place_id": place_id},
    )
    assert response.status_code == 200, response.text
    row = _feedback_row(response.json()["data"]["id"])
    assert row is not None
    assert row["place_id"] == place_id
    # 前后空白去掉：用户为了排版敲的空格不该成为数据的一部分
    assert row["message"] == "这份价格是错的"


def test_feedback_with_unknown_place_is_place_not_found(client: TestClient) -> None:
    """★ 语义必须是"这个地点找不到"，而不是撞外键后变成 500 ★"""
    response = _post_feedback(client, {"category": "closed", "place_id": str(uuid.uuid4())})
    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "PLACE_NOT_FOUND"


def test_feedback_with_unknown_trip_is_trip_not_found(client: TestClient) -> None:
    response = _post_feedback(client, {"category": "bad_route", "trip_id": str(uuid.uuid4())})
    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "TRIP_NOT_FOUND"


def test_feedback_rejects_unknown_category_and_bad_uuid(client: TestClient) -> None:
    bad_category = _post_feedback(client, {"category": "wrong_name"})
    assert bad_category.status_code == 422
    assert bad_category.json()["error"]["code"] == "INVALID_INPUT"

    bad_uuid = _post_feedback(client, {"category": "other", "place_id": "not-a-uuid"})
    assert bad_uuid.status_code == 422
    assert bad_uuid.json()["error"]["code"] == "INVALID_INPUT"


def test_feedback_rejects_overlong_message_instead_of_truncating(client: TestClient) -> None:
    limit = get_limits_config().rate_limit.feedback_message_max_chars
    response = _post_feedback(client, {"category": "other", "message": "长" * (limit + 1)})
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "INVALID_INPUT"
    # 必须说清上限，否则用户只能一次次试
    assert error["context"]["max_chars"] == limit


def test_feedback_rate_limit_returns_429_with_a_definite_retry(client: TestClient) -> None:
    limit = get_limits_config().rate_limit.feedback_per_ip_per_hour
    for _ in range(limit):
        assert _post_feedback(client, {"category": "other"}).status_code == 200

    blocked = _post_feedback(client, {"category": "other"})
    assert blocked.status_code == 429, blocked.text
    error = blocked.json()["error"]
    assert error["code"] == "RATE_LIMITED"
    assert error["context"]["retry_after_s"] > 0  # "随便等一会儿"不是一句可执行的提示


# ── POST /errors ────────────────────────────────────────────────────────────


def test_error_report_is_recorded_and_sanitized(client: TestClient) -> None:
    response = _post_error(
        client,
        {
            "component": "trip-page",
            "message": "渲染失败：请求头 Authorization: Bearer sk-abcdefghijklmnopqrst 被拒绝",
            "code": "RENDER_FAILED",
            "context": {"where": "/trip/abc", "token": "sk-abcdefghijklmnopqrst"},
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["recorded"] is True
    # 不回显输入：把不可信内容再送回浏览器一遍没有任何用处
    assert "message" not in data and "context" not in data

    row = _error_row(int(data["id"]))
    assert row is not None
    assert row["component"] == "trip-page"
    assert "sk-" not in row["message"]
    assert "sk-" not in (row["context"] or {}).get("token", "")


def test_error_report_rejects_empty_and_overlong_message(client: TestClient) -> None:
    assert _post_error(client, {"component": "x", "message": ""}).status_code == 422
    assert _post_error(client, {"component": "x", "message": "  "}).status_code == 422

    limit = get_limits_config().rate_limit.error_report_max_chars
    response = _post_error(client, {"component": "x", "message": "错" * (limit + 1)})
    assert response.status_code == 422
    assert response.json()["error"]["context"]["max_chars"] == limit


def test_error_report_accepts_a_request_id_and_rejects_a_broken_one(client: TestClient) -> None:
    ok = _post_error(
        client,
        {
            "component": "x",
            "message": "boom",
            "request_id": str(uuid.uuid4()),
            "level": "warning",
        },
    )
    assert ok.status_code == 200, ok.text
    row = _error_row(int(ok.json()["data"]["id"]))
    assert row is not None and row["level"] == "warning"

    broken = _post_error(client, {"component": "x", "message": "boom", "request_id": "nope"})
    assert broken.status_code == 422
    assert broken.json()["error"]["code"] == "INVALID_INPUT"
