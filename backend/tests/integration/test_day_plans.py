"""按天设置（节奏 / 主题）的集成测试（真实数据库 + 真实知识库）。

为什么单独立一个文件：这是**用户逐天做的显式选择**，而它最容易退化成一个只改了标签、
没改算法的开关 —— 界面上写着「第 2 天＝文化日」，排出来的却是同一批站点。
所以这里量的是三件可核验的事：

1. 主题日真的改变了**那一天的站点**（按结果页上能看到的推荐理由判定）；
2. 每天的节奏真的各用各的（对**每一站**重算一遍它该有的停留时长）；
3. 主题做不到时**说出来**（与自由文本冲突、或凑不齐），而不是悄悄排成普通的一天。

判定「这站算不算主题」刻意复用前端能看到的 ``why_recommended``：如果后台认为它是
文化站点、而卡片上没写「文化」，那这条断言就该红 —— 那是同一件事的两个口径。
"""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import Mapping, Sequence
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import get_limits_config, get_scoring_config, get_settings
from app.db.models import RateLimitCounter
from app.domain.candidates import stay_duration_min
from app.domain.models import Pace, Place

pytestmark = pytest.mark.integration

_counter = itertools.count(8100)


async def _with_engine(run: Any) -> Any:
    engine = create_async_engine(get_settings().database_url)
    try:
        return await run(engine)
    finally:
        await engine.dispose()


def _clear_rate_limits() -> None:
    async def run(engine: Any) -> None:
        async with engine.begin() as conn:
            await conn.execute(delete(RateLimitCounter))

    asyncio.run(_with_engine(run))


def _place_baselines(place_ids: Sequence[str]) -> dict[str, tuple[str, int | None]]:
    """从库里取这些地点的 ``(category, recommended_duration_min)``。

    停留时长的判据是「地点自带时长 > 类别基线」，两者都在库里 —— 要验证
    \"这一站的停留等于**当天**节奏算出来的值\"，就得把这两个输入拿回来自己算一遍，
    而不是反过来用结果去凑一个能过的等式。
    """

    async def run(engine: Any) -> dict[str, tuple[str, int | None]]:
        async with engine.connect() as conn:
            rows = await conn.execute(
                text(
                    "SELECT id::text, category, recommended_duration_min FROM places "
                    "WHERE id::text = ANY(:ids)"
                ),
                {"ids": list(place_ids)},
            )
            return {row[0]: (row[1], row[2]) for row in rows}

    return cast("dict[str, tuple[str, int | None]]", asyncio.run(_with_engine(run)))


@pytest.fixture(autouse=True)
def _fresh_rate_limits() -> None:
    _clear_rate_limits()


def _payload(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "city": "guangzhou",
        "days": 2,
        "day_span": "full_day",
        "people": 2,
        "preferences": [],
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


def _theme_warnings(route: dict[str, Any]) -> list[str]:
    return [
        str(item["message"])
        for item in route["feasibility"]["warnings"]
        if str(item.get("code", "")).startswith("THEME")
    ]


def _stops_of(route: dict[str, Any], day: int) -> list[dict[str, Any]]:
    return [stop for stop in route["stops"] if int(stop.get("day") or 1) == day]


def _theme_label(key: str) -> str:
    return get_scoring_config().preference_dimensions[key].label


def _badge_ratio(route: dict[str, Any], day: int, theme: str) -> tuple[int, int]:
    """那一天有几站的推荐理由里写着这个主题（= 界面上能看到的证据）。"""
    label = _theme_label(theme)
    stops = _stops_of(route, day)
    matched = sum(1 for stop in stops if label in (stop.get("why_recommended") or ""))
    return matched, len(stops)


# ── 主题 ────────────────────────────────────────────────────────────────────


def test_theme_day_steers_that_day_and_not_the_other(client: TestClient) -> None:
    """★ 本轮的主修：第 1 天美食、第 2 天文化，两天各自真的围绕自己的主题 ★"""
    ratio_min = get_limits_config().planning.theme_day.min_ratio
    trip = _plan(
        client,
        day_plans=[
            {"pace": "balanced", "theme": "food"},
            {"pace": "balanced", "theme": "culture"},
        ],
    )

    for route in trip["routes"]:
        for day, theme in ((1, "food"), (2, "culture")):
            matched, total = _badge_ratio(route, day, theme)
            assert total, f"{route['label']} 的第 {day} 天没有站点"
            assert matched / total >= ratio_min, (
                f"{route['label']} 第 {day} 天只有 {matched}/{total} 站是"
                f"「{_theme_label(theme)}」（要求 ≥{ratio_min:.0%}）："
                f"{[(s['name'], s.get('why_recommended')) for s in _stops_of(route, day)]}"
            )
        # 主题不能靠"两天都去同一批地点"实现：跨天仍不重复
        day1 = {stop["place_id"] for stop in _stops_of(route, 1)}
        day2 = {stop["place_id"] for stop in _stops_of(route, 2)}
        assert not (day1 & day2), f"{route['label']} 第 2 天又去了第 1 天去过的地方"


def test_theme_day_is_dropped_when_free_text_says_no(client: TestClient) -> None:
    """主题与「不要文化」冲突时：**不偷偷按主题排**，而是在「需要留意」里说清。

    用户说了两件互相矛盾的事，正确做法不是替他挑一件 —— 而是照更明确的那句做，
    再把冲突写出来（那一天的站点于是可以是任何主题）。
    """
    trip = _plan(
        client,
        day_plans=[
            {"pace": "balanced", "theme": "culture"},
            {"pace": "balanced"},
        ],
        free_text=f"预算 {next(_counter)} 元，不要文化",
    )

    for route in trip["routes"]:
        notes = _theme_warnings(route)
        assert any("不要文化" in note for note in notes), route["feasibility"]["warnings"]
        # 必须在「需要留意」里（而不是只躺在报告深处）：这是用户选了、我们没做到的事
        assert any("不要文化" in con for con in route["cons"]), route["cons"]
        # 而且真的没按主题排
        matched, total = _badge_ratio(route, 1, "culture")
        assert matched == 0, f"说了不要文化，第 1 天还是有 {matched}/{total} 站标着文化"


# ── 节奏 ────────────────────────────────────────────────────────────────────


def _assert_stays_follow_the_day_pace(
    route: dict[str, Any], pace_of_day: Mapping[int, Pace]
) -> int:
    """逐站核对\"停留 = 地点基线 × 当天节奏\"，返回核对的站数。

    刻意逐站重算（类别/自带时长从库里取回来），而不是比两天的平均停留：
    平均值受\"这两天分别排到了哪些地点\"影响，相等或反超都可能是巧合 ——
    那样的测试红得莫名其妙、绿得也没有说服力。
    """
    if route["route_source"] == "template":
        # 模板路线自带**策展的**停留分钟，不按节奏缩放（见 TASKS.md 的按天设置一节），
        # 所以不在这条断言的范围内。这是已知缺口，而不是这里没测。
        return 0
    baselines = _place_baselines([stop["place_id"] for stop in route["stops"]])
    checked = 0
    for stop in route["stops"]:
        day = int(stop.get("day") or 1)
        category, base = baselines[stop["place_id"]]
        expected = stay_duration_min(
            Place(
                id=stop["place_id"],
                name=stop["name"],
                category=category,
                lat=0.0,
                lng=0.0,
                recommended_duration_min=base,
            ),
            pace_of_day[day],
            limits=get_limits_config(),
        )
        assert int(stop["stay_min"]) == expected, (
            f"第 {day} 天（{pace_of_day[day]}）的 {stop['name']} 停留 "
            f"{stop['stay_min']} 分钟，按当天节奏应为 {expected} 分钟"
        )
        checked += 1
    return checked


def test_each_day_uses_its_own_pace_for_every_stop(client: TestClient) -> None:
    """★ 第 1 天轻松、第 2 天紧凑：两天的每一站都按**各自**的节奏算停留 ★"""
    trip = _plan(
        client,
        day_plans=[
            {"pace": "relaxed", "theme": "food"},
            {"pace": "packed", "theme": "culture"},
        ],
    )

    checked = sum(
        _assert_stays_follow_the_day_pace(route, {1: "relaxed", 2: "packed"})
        for route in trip["routes"]
    )
    assert checked >= 6, "没有可校验的站点，这条断言等于没跑"


def test_shorter_day_plans_fall_back_to_the_single_pace(client: TestClient) -> None:
    """只给第 1 天设置时，其余天用 ``pace`` —— 不传 day_plans 的旧调用方完全不受影响。"""
    trip = _plan(client, days=2, day_plans=[{"pace": "packed"}], pace="relaxed")

    assert trip["intent"]["day_plans"] == [{"day": 1, "pace": "packed", "theme": None}]
    checked = sum(
        _assert_stays_follow_the_day_pace(route, {1: "packed", 2: "relaxed"})
        for route in trip["routes"]
    )
    assert checked >= 6


# ── 入参校验与快照 ──────────────────────────────────────────────────────────


def test_day_plans_reach_the_intent_snapshot(client: TestClient) -> None:
    """按天设置要进意图快照：否则日后没人能解释\"第 2 天为什么全是文化站点\"。"""
    trip = _plan(
        client,
        day_plans=[
            {"pace": "relaxed", "theme": "food"},
            {"pace": "packed", "theme": "culture"},
        ],
    )

    assert trip["intent"]["day_plans"] == [
        {"day": 1, "pace": "relaxed", "theme": "food"},
        {"day": 2, "pace": "packed", "theme": "culture"},
    ]


def test_unknown_theme_is_rejected(client: TestClient) -> None:
    """未知主题必须报错：静默退回\"没主题\"会让用户以为自己选上了。"""
    response = client.post(
        "/api/v1/trips:plan",
        params={"sync": "true"},
        json=_payload(day_plans=[{"pace": "relaxed", "theme": "潜水"}]),
    )

    assert response.status_code == 422
    assert "主题" in response.text


def test_more_day_plans_than_days_is_rejected(client: TestClient) -> None:
    """多设的天永远不会被排到 —— 说出来，别静默截断。"""
    response = client.post(
        "/api/v1/trips:plan",
        params={"sync": "true"},
        json=_payload(days=1, day_plans=[{"pace": "relaxed"}, {"pace": "packed"}]),
    )

    assert response.status_code == 422
    assert "天" in response.text


def test_day_plans_are_part_of_the_cache_key(client: TestClient) -> None:
    """★ 同参数、不同按天设置，必须是两套行程 ★

    指纹（``params_hash``）漏掉按天设置时，第二趟会**直接命中第一趟的缓存**：
    用户改了第 2 天的主题，看到的却是没改的那版，而所有其它测试仍然是绿的。
    """
    free_text = f"预算 {next(_counter)} 元"
    first = _plan(
        client,
        free_text=free_text,
        day_plans=[
            {"pace": "relaxed", "theme": "food"},
            {"pace": "relaxed", "theme": "food"},
        ],
    )
    same = _plan(
        client,
        free_text=free_text,
        day_plans=[
            {"pace": "relaxed", "theme": "food"},
            {"pace": "relaxed", "theme": "food"},
        ],
    )
    changed = _plan(
        client,
        free_text=free_text,
        day_plans=[
            {"pace": "relaxed", "theme": "food"},
            {"pace": "relaxed", "theme": "night_view"},
        ],
    )

    assert same["trip_id"] == first["trip_id"], "同样的请求应当复用同一版行程"
    assert changed["trip_id"] != first["trip_id"], "改了第 2 天的主题却拿到同一版行程"
