"""多日行程与「每天玩多久」的集成测试（真实数据库 + 真实知识库）。

守的是一件被用户一眼看穿的事：**选 2 天却只拿到一天的行程**。
``days`` 此前只进了行程标题 —— 规划器从不读它，所以「2 天行程」的每一站都发生在同一天，
而窗口 09:00–21:00 里只排了 4–5 小时（站点数被 archetype 的 ``max_stops`` 封顶，
relaxed 只给 4 站）。本地人"只想玩个半天"同样没有入口，只能手动把结束时间改到中午。

这里断言的都是**可核验的量**（天数、每天的起止时刻、跨天是否重复），
而不是"看起来够满"这类说不清的标准。
"""

from __future__ import annotations

import asyncio
import itertools
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import get_limits_config, get_settings
from app.db.models import RateLimitCounter

pytestmark = pytest.mark.integration

_counter = itertools.count(7100)


def _clear_rate_limits() -> None:
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


def _payload(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "city": "guangzhou",
        "days": 1,
        "day_span": "full_day",
        "people": 2,
        # 不带偏好：多日去重要在"全部候选"里找不重复的地点，偏好越窄越容易排不满
        "preferences": [],
        "pace": "relaxed",
        "budget": {"amount": 2000, "scope": "per_person"},
        # 每次换一个可解析的预算值 ⇒ 不同的 params_hash，用例之间不会命中彼此缓存
        "free_text": f"预算 {next(_counter)} 元",
    }
    base.update(overrides)
    return base


def _plan(client: TestClient, **overrides: object) -> dict[str, Any]:
    response = client.post(
        "/api/v1/trips:plan", params={"sync": "true"}, json=_payload(**overrides)
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True, body
    return cast("dict[str, Any]", body["data"])


def _days_of(route: dict[str, Any]) -> dict[int, list[dict[str, Any]]]:
    grouped: dict[int, list[dict[str, Any]]] = {}
    for stop in route["stops"]:
        grouped.setdefault(int(stop.get("day") or 1), []).append(stop)
    return grouped


def _minutes(hhmm: str) -> int:
    hour, minute = hhmm.split(":")
    return int(hour) * 60 + int(minute)


def _span_minutes(stops: list[dict[str, Any]]) -> int:
    return _minutes(stops[-1]["depart_time"]) - _minutes(stops[0]["arrive_time"])


def test_single_day_is_unchanged(client: TestClient) -> None:
    """单日行程的站点必须全部标在第 1 天 —— 旧行为不能被这次改动静默改掉。"""
    trip = _plan(client, days=1)
    assert trip["days"] == 1
    for route in trip["routes"]:
        assert all(int(stop["day"]) == 1 for stop in route["stops"])


def test_two_days_actually_produce_two_distinct_days(client: TestClient) -> None:
    """★ 本轮的主修：选 2 天就要拿到 2 天，而且跨天不重复同一地点 ★"""
    limits = get_limits_config()
    trip = _plan(client, days=2)
    assert trip["days"] == 2, "标题写 2 天、内容只有 1 天就是本轮要修的那个 bug"

    for route in trip["routes"]:
        grouped = _days_of(route)
        assert set(grouped) == {1, 2}, f"{route['label']} 只排出了 {sorted(grouped)} 天"
        seen: set[str] = set()
        for day, stops in grouped.items():
            assert stops, f"第 {day} 天没有任何站点"
            for stop in stops:
                assert stop["place_id"] not in seen, (
                    f"{stop['name']} 在两天里各去了一次（跨天去重失效）"
                )
                seen.add(stop["place_id"])
        # 每天都不许超过"一天的配额"：超过说明 day_span 没生效
        for stops in grouped.values():
            assert _span_minutes(stops) <= limits.planning.day_span_minutes["full_day"]


def test_half_day_really_ends_around_half_a_day(client: TestClient) -> None:
    """本地人「玩个半天」：每天的实际时长必须短于一天档，且不超过半天的配额。"""
    limits = get_limits_config()
    half = _plan(client, day_span="half_day")
    full = _plan(client, day_span="full_day")

    half_day_spans = [_span_minutes(stops) for route in half["routes"] for stops in _days_of(route).values()]
    assert half_day_spans, "半天档一条站点都没有，断言无法成立"
    assert max(half_day_spans) <= limits.planning.day_span_minutes["half_day"], half_day_spans
    # 半天也要是一趟"行程"：每天至少 min_route_stops 站，否则这一档等于排不出东西
    for route in half["routes"]:
        for stops in _days_of(route).values():
            assert len(stops) >= limits.planning.min_route_stops, stops

    full_day_spans = [
        _span_minutes(stops) for route in full["routes"] for stops in _days_of(route).values()
    ]
    assert max(full_day_spans) > max(half_day_spans), (
        "换了游玩时长却排得一样长，说明这档设置没有生效"
    )


def test_whole_window_can_exceed_a_full_day(client: TestClient) -> None:
    """「尽可能多」用满用户给的时间窗：它可以比"一天"档更长。"""
    limits = get_limits_config()
    full_day_quota = limits.planning.day_span_minutes["full_day"]
    trip = _plan(client, day_span="whole_window")

    spans = [_span_minutes(stops) for route in trip["routes"] for stops in _days_of(route).values()]
    assert spans
    assert max(spans) > full_day_quota, (
        "「尽可能多」与「一天」排得一样长：说明时间窗没有成为那一档的依据"
    )


def test_the_plan_fills_more_of_the_window_than_before(client: TestClient) -> None:
    """一整天的窗户是 12 小时（09:00–21:00），别只排 4–5 小时就收工。

    这条是用户最初看到的现象本身（"选了两天/三天，方案总时长很短"）：
    站点数过去只由 ``max_stops`` 封顶，于是真正的瓶颈被写死在配置里，
    而时间窗看起来还很空。这里只断言"用掉了窗口的大头"，
    不去钉具体站数（那会随评分权重变化而漂）。
    """
    trip = _plan(client, days=1, day_span="whole_window")
    best = max(
        _span_minutes(stops) for route in trip["routes"] for stops in _days_of(route).values()
    )
    window = get_limits_config().planning.default_window
    window_min = _minutes(window["end"]) - _minutes(window["start"])
    assert best >= window_min * 0.7, f"12 小时的窗口只排了 {best} 分钟"


def test_shared_trip_keeps_the_day_labels(client: TestClient) -> None:
    """分享链接也要按天分组：分享页读的是同一份落库数据，不能只有主流程是对的。"""
    trip = _plan(client, days=2)
    # 分享必须用**创建它的那个会话**：行程按会话隔离，换一个会话连分享都拿不到
    response = client.post(
        f"/api/v1/trips/{trip['trip_id']}/share", json={"public": True}
    )
    assert response.status_code == 200, response.text
    slug = response.json()["data"]["slug"]

    public = client.get(f"/api/v1/public/trips/{slug}")
    assert public.status_code == 200, public.text
    shared = public.json()["data"]
    assert shared["days"] == 2
    assert any(int(stop["day"]) > 1 for route in shared["routes"] for stop in route["stops"])


def test_requested_and_actual_days_are_both_visible(client: TestClient) -> None:
    """需求里的天数与**实际排出来**的天数分开记录：素材不够时如实少排，而不是假装排满。"""
    trip = _plan(client, days=3)
    assert trip["intent"]["days"] == 3, "快照里的 days 必须是用户请求的天数"
    # 实际天数可能少于请求值（由素材够不够决定），但两者都要能读到
    assert 1 <= trip["days"] <= 3
    assert "days_composed" in trip["intent"]
    assert trip["intent"]["days_composed"] == trip["days"]


def test_day_span_reaches_the_stored_intent(client: TestClient) -> None:
    """游玩时长要进意图快照：否则日后没人能解释"为什么 12 小时的窗口只排了 4 小时"。"""
    trip = _plan(client, day_span="half_day")
    assert trip["intent"]["day_span"] == "half_day"


def test_invalid_day_span_is_rejected(client: TestClient) -> None:
    """非法取值必须报错，而不是静默退回默认值（那会让用户以为选上了）。"""
    response = client.post(
        "/api/v1/trips:plan", params={"sync": "true"}, json=_payload(day_span="whenever")
    )
    assert response.status_code == 422
