"""规划与行程 API 集成测试（PRD §7.2 / FR-08 / FR-09 / FR-13）。

这是 M4 的"证据"文件：规划编排、SSE、幂等、修改/撤销、分享全部跑在**真实数据库
与真实知识库**上，而不是用 mock 拼出来的假成功。

守的关键行为：
- 一次同步规划必须产出 ≥2 套**通过可行性校验**的方案（含站点时刻、来源、估算标注）；
- 同 session 同参数 10 秒内重复提交返回同一个 trip（幂等，不重复计费）；
- 行程归属：换一个会话既读不到也改不了（trip_id 不是访问凭据）；
- 修改走本地重算并产出可读 Diff；理解不了就回问澄清而不是报错；
- 分享链接可免登录查看，取消后**立刻 404**；复制得到的是新行程。

限流计数器是"状态"而不是业务数据：每个用例前清空，否则整套用例会共用同一天的
配额（默认每 IP 5 次冷规划），第一次跑通过、第二次跑全被 429 挡住。
"""

from __future__ import annotations

import asyncio
import itertools
import uuid
from collections.abc import Callable
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import get_limits_config, get_settings
from app.db.models import RateLimitCounter
from app.services.session import encode_session_cookie

pytestmark = pytest.mark.integration


def _clear_rate_limits() -> None:
    # conftest 在导入应用模块之前就把 DATABASE_URL 指向测试库了，
    # 所以这里读到的一定是测试库（而不是开发库）。
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





_counter = itertools.count(1000)


def _payload(**overrides: object) -> dict[str, object]:
    """每次生成独立的参数。

    ★ 为什么必须让**解析后的参数**不同，而不只是 free_text 不同 ★
    缓存/幂等键是 ``params_hash``，它由解析出的 intent 与约束构成 ——
    “想轻松一点” 里没有任何可解析的量，换一个随机后缀得到的还是同一个键，
    于是用例之间会命中彼此的缓存（第一版就是这么写错的）。
    这里改用一个**能解析成预算**的句子，让每个用例真的落在不同的参数上。
    """
    base: dict[str, object] = {
        "city": "guangzhou",
        "days": 1,
        "people": 2,
        "preferences": ["food"],
        "pace": "relaxed",
        "budget": {"amount": 400, "scope": "per_person"},
        "free_text": f"预算 {next(_counter)} 元",
    }
    base.update(overrides)
    return base


def _plan_sync(
    client: TestClient, cookies: dict[str, str] | None = None, **overrides: object
) -> dict[str, Any]:
    response = client.post(
        "/api/v1/trips:plan",
        params={"sync": "true"},
        json=_payload(**overrides),
        cookies=cookies,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True, body
    return cast("dict[str, Any]", body["data"])


def _other_session() -> dict[str, str]:
    """另一个访客会话的合法 cookie（本项目已验证过这个写法是稳定的）。"""
    return {"td_session": encode_session_cookie(uuid.uuid4())}


def _revise(client: TestClient, trip_id: str, instruction: str, cookies: dict[str, str] | None = None) -> dict[str, Any]:
    response = client.post(
        f"/api/v1/trips/{trip_id}/revise", json={"instruction": instruction}, cookies=cookies
    )
    assert response.status_code == 200, response.text
    return cast("dict[str, Any]", response.json()["data"])


# ── 规划 ────────────────────────────────────────────────────────────────────


def test_sync_plan_returns_feasible_routes(client: TestClient) -> None:
    trip = _plan_sync(client)
    assert trip["route_count"] >= 2, "PRD 要求至少 2 套合理方案"
    assert len(trip["routes"]) == trip["route_count"]
    assert trip["city"] == "guangzhou"
    assert trip["total_cost_cny"] == "0", "只读本地知识库，外部成本必须为 0"

    for route in trip["routes"]:
        assert route["place_count"] >= 3
        assert route["feasibility"]["feasible"] is True
        assert route["recommend_score"] > 0
        assert len(route["stops"]) == route["place_count"]
        # AC-6.2：优点 ≥2 条、缺点 ≥1 条、推荐理由不能空
        assert len(route["pros"]) >= 2
        assert len(route["cons"]) >= 1
        assert route["recommendation_reason"]
        for stop in route["stops"]:
            assert stop["name"], "站点必须有名字（快照里的地点名）"
            assert stop["arrive_time"] <= stop["depart_time"]
            assert stop["transport_source"] in (None, "amap", "osrm", "estimated", "manual")


def test_routes_cover_distinct_archetypes(client: TestClient) -> None:
    """★ FR-06：三套方案定位必须不同 ★

    纯按总分取前三名时，实测会输出三套都是"经典"—— 标签、适合人群、
    推荐理由全部雷同，用户看到的三个 Tab 其实是同一条路线的三种摆法。
    """
    trip = _plan_sync(client)
    archetypes = [route["archetype"] for route in trip["routes"]]
    assert len(set(archetypes)) == len(archetypes), f"三套方案雷同：{archetypes}"
    best_for = [tuple(route["best_for"]) for route in trip["routes"]]
    assert len(set(best_for)) == len(best_for), f"适合人群没有区分度：{best_for}"


def test_routes_are_not_too_similar(client: TestClient) -> None:
    """AC-6.1：两两地点集合的 Jaccard ≤ 0.7。"""
    trip = _plan_sync(client)
    sets = [{stop["place_id"] for stop in route["stops"]} for route in trip["routes"]]
    for i, left in enumerate(sets):
        for right in sets[i + 1 :]:
            union = left | right
            similarity = len(left & right) / len(union) if union else 0.0
            assert similarity <= 0.7, f"方案过于雷同（{similarity:.2f}）"


def test_sync_plan_is_honest_about_estimates(client: TestClient) -> None:
    """时间/距离是估算值时必须带标注，且降级模式要如实返回。"""
    trip = _plan_sync(client)
    assert trip["degraded_modes"], "无 Key 环境必须报告降级模式"
    first = trip["routes"][0]
    assert first["budget_estimated"] is True
    sources = {stop["transport_source"] for stop in first["stops"] if stop["transport_source"]}
    assert sources <= {"amap", "osrm", "estimated", "manual"}


def test_global_daily_budget_switches_plans_to_local_only(
    client: TestClient, cost_row_factory: Callable[[str], int]
) -> None:
    """★ PRD §15.4 第四级熔断：全局日成本超限 ⇒ 进「缓存优先模式」，不再调模型/联网。

    这一级以前**没有任何代码读它**：`config/limits.yaml` 里写着 20 元，
    但没人比对 —— 一个拦不住支出的阈值比没有更危险，看到它在那里就会以为有兜底。
    而“单次规划”的熔断器只看本次，看不住一天里很多次堆起来的账单。

    这里连降级原因一起断言：钱花完了才降级，与“本来就没配 Key”是两件事，
    不说清楚就只能靠猜。
    """
    limit = get_limits_config().cost.global_daily_cny
    cost_row_factory(str(float(limit) + 5))
    trip = _plan_sync(client)

    reasons = [mode for mode in trip["degraded_modes"] if mode.startswith("cost:global_daily_budget")]
    assert reasons, f"已超预算却没有报告：{trip['degraded_modes']}"
    # 金额与上限都要写出来：“超预算了”没用，“花了 25/20”才有用
    assert str(round(float(limit) + 5, 4)) in reasons[0]
    assert f"{limit:.2f}" in reasons[0]


def test_revise_applies_intent_only_instruction(client: TestClient) -> None:
    """★ 只改"意图"、不产生任何约束的指令必须真的生效 ★

    "改成 3 天"由规则引擎写进 ``intent.days``，不产生 Constraint。
    早期实现只把约束传给重算、丢掉解析后的意图，于是它既不报错也不改任何东西，
    却返回一个新版本 + 一句"什么都没变"的 Diff —— 用户的要求被静默丢弃。
    """
    trip = _plan_sync(client, free_text="预算 2101 元")
    assert trip["days"] == 1

    data = _revise(client, trip["trip_id"], "改成 3 天")
    assert data["trip_id"] != trip["trip_id"]
    assert data["revision_no"] == 2
    assert data["diff"]["days_before"] == 1
    assert data["diff"]["days_after"] == 3
    assert "天数" in data["diff"]["sentence"], "Diff 必须说出改了什么"

    new_trip = client.get(f"/api/v1/trips/{data['trip_id']}").json()["data"]
    assert new_trip["days"] == 3
    assert new_trip["revision_no"] == 2


def test_revise_applies_budget_change(client: TestClient) -> None:
    """预算同理：只改数值字段的指令不能被丢掉。"""
    trip = _plan_sync(client, free_text="预算 2102 元")
    data = _revise(client, trip["trip_id"], "预算改成 900 元")

    new_trip = client.get(f"/api/v1/trips/{data['trip_id']}").json()["data"]
    assert new_trip["intent"]["budget"]["amount"] == "900"


def test_revise_does_not_replay_the_original_free_text(client: TestClient) -> None:
    """修改时不能把原始自由文本重放一遍：那会把用户这次的改动覆盖回去。"""
    trip = _plan_sync(client, free_text="预算 2103 元")
    data = _revise(client, trip["trip_id"], "预算改成 800 元")
    new_trip = client.get(f"/api/v1/trips/{data['trip_id']}").json()["data"]
    assert new_trip["intent"]["budget"]["amount"] == "800", "不能被原文里的 2103 覆盖回 2103"


def test_plan_exclusion_changes_the_route(client: TestClient) -> None:
    """排除一个真实地点必须真的把它从方案里去掉（而不是只在文案里说排除）。"""
    base = _plan_sync(client)
    target = base["routes"][0]["stops"][0]["name"]
    revised = _plan_sync(client, free_text=f"不要去{target} {uuid.uuid4().hex[:6]}")
    names = {stop["name"] for stop in revised["routes"][0]["stops"]}
    assert target not in names


def test_plan_sets_session_cookie(client: TestClient) -> None:
    response = client.post("/api/v1/trips:plan", params={"sync": "true"}, json=_payload())
    assert response.status_code == 200
    assert "td_session=" in response.headers.get("set-cookie", "")


def test_plan_is_idempotent_within_window(client: TestClient) -> None:
    payload = _payload(free_text="预算 2002 元")
    first = client.post("/api/v1/trips:plan", params={"sync": "true"}, json=payload).json()
    second = client.post("/api/v1/trips:plan", params={"sync": "true"}, json=payload).json()
    assert first["data"]["trip_id"] == second["data"]["trip_id"]
    assert second["meta"]["cached"] is True, "重复提交必须走幂等而不是重新生成"


def test_plan_cache_is_reused_across_sessions_by_copying(client: TestClient) -> None:
    """★ 跨会话复用：命中别人的缓存时**复制一份**，而不是把 trip_id 交出去 ★

    trip_id 是他人行程的访问凭据。直接把缓存里的 id 返回给另一个会话，
    等于把别人的行程"送"出去（即使参数相同）。正确做法是本会话生成一个新 trip。
    """
    payload = _payload(free_text="预算 2001 元")  # 固定文本 → 两次请求的参数完全一致
    first = client.post("/api/v1/trips:plan", params={"sync": "true"}, json=payload).json()
    assert first["meta"]["cached"] is False

    other_session = {"td_session": encode_session_cookie(uuid.uuid4())}
    second = client.post(
        "/api/v1/trips:plan",
        params={"sync": "true"},
        json=payload,
        cookies=other_session,
    ).json()
    assert second["ok"] is True, second
    assert second["meta"]["cached"] is True, "第二次必须命中缓存，不重复计算"
    assert second["data"]["trip_id"] != first["data"]["trip_id"], "不能把别人的 trip_id 交给当前会话"
    assert len(second["data"]["routes"]) == len(first["data"]["routes"])

    # 副本属于当前会话：换回去拿不到它，用自己有权限
    assert client.get(f"/api/v1/trips/{second['data']['trip_id']}", cookies=other_session).status_code == 200


def test_repeat_plan_after_cache_hit_is_idempotent(client: TestClient) -> None:
    """★ 幂等必须覆盖"命中别人缓存后复制出来的行程" ★

    副本的 ``TripRequest`` 早期既没写 ``elapsed_ms``，又用意图快照重算了一个
    （还漏掉约束的）``params_hash``，于是它对幂等重放不可见：同一个会话重复提交
    同一份需求，每次都重新复制一份，库里堆出重复行程（AC-13.4 失效）。
    """
    payload = _payload(free_text="预算 2104 元")
    client.post("/api/v1/trips:plan", params={"sync": "true"}, json=payload)
    other = _other_session()

    first = client.post(
        "/api/v1/trips:plan", params={"sync": "true"}, json=payload, cookies=other
    ).json()
    second = client.post(
        "/api/v1/trips:plan", params={"sync": "true"}, json=payload, cookies=other
    ).json()

    assert second["data"]["trip_id"] == first["data"]["trip_id"], "重复提交必须返回同一个 trip"
    assert second["meta"]["cached"] is True


def test_plan_rejects_unknown_city(client: TestClient) -> None:
    response = client.post(
        "/api/v1/trips:plan", params={"sync": "true"}, json=_payload(city="shenzhen")
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "UNSUPPORTED_CITY"


def test_plan_rejects_unknown_preference(client: TestClient) -> None:
    response = client.post(
        "/api/v1/trips:plan", params={"sync": "true"}, json=_payload(preferences=["潜水"])
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_INPUT"


# ── 读取与归属 ──────────────────────────────────────────────────────────────


def test_get_trip_round_trip(client: TestClient) -> None:
    trip = _plan_sync(client)
    response = client.get(f"/api/v1/trips/{trip['trip_id']}")
    assert response.status_code == 200
    assert response.json()["data"]["trip_id"] == trip["trip_id"]


def test_trip_is_isolated_per_session(client: TestClient) -> None:
    """★ 行程归属：换一个会话既读不到也改不了 ★

    刻意**不**新建一个 TestClient：每个 TestClient 都有自己的事件循环，
    而复用的 asyncpg 连接池绑定在第一个循环上，第二个客户端会撞上
    "attached to a different loop"（本项目已经踩过同一个坑，见 TASKS.md #15）。
    改用**另一个会话的合法 cookie** 发同一个客户端的请求，语义一样但稳定。
    """
    trip = _plan_sync(client)
    other_session = {"td_session": encode_session_cookie(uuid.uuid4())}

    assert (
        client.get(f"/api/v1/trips/{trip['trip_id']}", cookies=other_session).status_code == 403
    )
    share = client.post(
        f"/api/v1/trips/{trip['trip_id']}/share", json={"public": True}, cookies=other_session
    )
    assert share.status_code == 403
    undo = client.post(f"/api/v1/trips/{trip['trip_id']}/undo", cookies=other_session)
    assert undo.status_code == 403


def test_get_unknown_trip_returns_404(client: TestClient) -> None:
    response = client.get(f"/api/v1/trips/{uuid.uuid4()}")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "TRIP_NOT_FOUND"


# ── SSE ─────────────────────────────────────────────────────────────────────


def test_async_plan_streams_progress_and_completion(client: TestClient) -> None:
    accepted = client.post("/api/v1/trips:plan", json=_payload())
    assert accepted.status_code == 202, accepted.text
    body = accepted.json()["data"]
    assert body["stream_url"].endswith("/stream")

    stream = client.get(body["stream_url"])
    assert stream.status_code == 200
    assert stream.headers["content-type"].startswith("text/event-stream")
    text = stream.text
    assert "plan.started" in text
    assert "plan.progress" in text, "必须能看到真实进度，而不是只有一个转圈"
    assert "plan.completed" in text

    completed_line = [line for line in text.splitlines() if line.startswith("data: ")][-1]
    assert body["request_id"] in text
    assert "trip_id" in completed_line


def test_stream_unknown_request_returns_404(client: TestClient) -> None:
    response = client.get(f"/api/v1/trips/{uuid.uuid4()}/stream")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "TRIP_NOT_FOUND"


# ── 修改与撤销 ──────────────────────────────────────────────────────────────


def test_revise_creates_new_revision_with_diff(client: TestClient) -> None:
    trip = _plan_sync(client)
    target = trip["routes"][0]["stops"][0]["name"]

    response = client.post(
        f"/api/v1/trips/{trip['trip_id']}/revise", json={"instruction": f"不要去{target}"}
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["trip_id"] != trip["trip_id"], "修改必须产出新版本，而不是原地改"
    assert data["revision_no"] == 2
    assert target in data["diff"]["removed"]
    assert data["diff"]["sentence"], "Diff 要有一句人话"
    assert data["needs_clarification"] is None

    new_trip = client.get(f"/api/v1/trips/{data['trip_id']}").json()["data"]
    assert {s["name"] for s in new_trip["routes"][0]["stops"]}.isdisjoint({target})


def test_revise_asks_for_clarification_instead_of_failing(client: TestClient) -> None:
    """理解不了就回问，而不是报错（PRD AC-8.7）。"""
    trip = _plan_sync(client)
    response = client.post(
        f"/api/v1/trips/{trip['trip_id']}/revise", json={"instruction": "嗯"}
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["trip_id"] == trip["trip_id"], "没理解就不该产出新版本"
    assert data["needs_clarification"]


def test_undo_returns_previous_version(client: TestClient) -> None:
    trip = _plan_sync(client)
    target = trip["routes"][0]["stops"][0]["name"]
    revised = client.post(
        f"/api/v1/trips/{trip['trip_id']}/revise", json={"instruction": f"不要去{target}"}
    ).json()["data"]

    undone = client.post(f"/api/v1/trips/{revised['trip_id']}/undo")
    assert undone.status_code == 200
    assert undone.json()["data"]["trip_id"] == trip["trip_id"]


def test_undo_without_history_is_rejected(client: TestClient) -> None:
    trip = _plan_sync(client)
    response = client.post(f"/api/v1/trips/{trip['trip_id']}/undo")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_INPUT"


def test_undo_after_revising_a_cache_copied_trip(client: TestClient) -> None:
    """★ 撤销不能走进原作者的行程 ★

    复制出来的 trip 的 ``parent_trip_id`` 指向**来源**（别人的 trip），不是上一版。
    早期实现把它当上一版并试图"跳过 plan_cache 父节点"，于是复制别人的缓存 →
    修改 → 撤销会沿父链走到作者那条 trip，撞上归属校验，用户拿到 403。
    """
    payload = _payload(free_text="预算 2105 元")
    original = client.post(
        "/api/v1/trips:plan", params={"sync": "true"}, json=payload
    ).json()["data"]
    other = _other_session()
    copied = client.post(
        "/api/v1/trips:plan", params={"sync": "true"}, json=payload, cookies=other
    ).json()["data"]
    assert copied["trip_id"] != original["trip_id"]

    target = copied["routes"][0]["stops"][0]["name"]
    revised = _revise(client, copied["trip_id"], f"不要去{target}", other)

    undone = client.post(f"/api/v1/trips/{revised['trip_id']}/undo", cookies=other)
    assert undone.status_code == 200, undone.text
    assert undone.json()["data"]["trip_id"] == copied["trip_id"], "撤销应回到本会话自己的上一版"


def test_undo_on_a_copied_trip_reports_no_history(client: TestClient) -> None:
    """副本没有更早的版本：必须说"已是最早的版本"，而不是 403 或别人的行程。"""
    payload = _payload(free_text="预算 2106 元")
    client.post("/api/v1/trips:plan", params={"sync": "true"}, json=payload)
    other = _other_session()
    copied = client.post(
        "/api/v1/trips:plan", params={"sync": "true"}, json=payload, cookies=other
    ).json()["data"]

    response = client.post(f"/api/v1/trips/{copied['trip_id']}/undo", cookies=other)
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "INVALID_INPUT"


def test_undo_on_copied_shared_trip_reports_no_history(client: TestClient) -> None:
    """「复制这套路线」同样是一个 fork 起点，不是上一版。"""
    trip = _plan_sync(client)
    slug = client.post(
        f"/api/v1/trips/{trip['trip_id']}/share", json={"public": True}
    ).json()["data"]["slug"]
    other = _other_session()
    copied = client.post(f"/api/v1/public/trips/{slug}/copy", cookies=other).json()["data"]

    response = client.post(f"/api/v1/trips/{copied['trip_id']}/undo", cookies=other)
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "INVALID_INPUT"


# ── 分享 ────────────────────────────────────────────────────────────────────


def test_share_public_view_and_revoke(client: TestClient) -> None:
    trip = _plan_sync(client)
    shared = client.post(f"/api/v1/trips/{trip['trip_id']}/share", json={"public": True})
    assert shared.status_code == 200
    share = shared.json()["data"]
    assert share["is_public"] is True
    assert len(share["slug"]) >= 10, "slug 必须不可枚举（PRD AC-9.1）"
    assert share["url"].endswith(f"/t/{share['slug']}")

    public = client.get(f"/api/v1/public/trips/{share['slug']}")
    assert public.status_code == 200
    assert public.json()["data"]["trip_id"] == trip["trip_id"]

    revoked = client.post(f"/api/v1/trips/{trip['trip_id']}/share", json={"public": False})
    assert revoked.json()["data"]["is_public"] is False
    assert client.get(f"/api/v1/public/trips/{share['slug']}").status_code == 404


def test_shared_trip_can_be_copied(client: TestClient) -> None:
    trip = _plan_sync(client)
    slug = client.post(
        f"/api/v1/trips/{trip['trip_id']}/share", json={"public": True}
    ).json()["data"]["slug"]

    copied = client.post(f"/api/v1/public/trips/{slug}/copy")
    assert copied.status_code == 201
    data = copied.json()["data"]
    assert data["trip_id"] != trip["trip_id"]
    assert len(data["routes"]) == len(trip["routes"])


def test_unknown_share_slug_returns_404(client: TestClient) -> None:
    response = client.get("/api/v1/public/trips/doesnotexist123")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "SHARE_NOT_FOUND"
