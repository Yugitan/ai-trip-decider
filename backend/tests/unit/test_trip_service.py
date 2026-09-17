"""行程读取/修改/撤销的单元测试（PRD FR-08 / FR-09）。

这三条规则都是对抗性审查里**真实复现**出来的静默出错路径，写在这里是因为
它们不碰数据库、只靠参数就能守住：

1. ``parent_trip_id`` 有两种含义 —— "上一版"（revision）与"来源"（复制）。
   混为一谈会把用户沿父链带进**别人的行程**：复制别人的缓存 → 修改 → 撤销，
   早期实现返回 403「这个行程不属于当前会话」，而不是回到用户自己的那一版。
2. 从意图快照重建规划入参时，权重 0 的偏好（用户明确说"不要拍照"）必须被丢掉，
   否则重建出来的请求会把它们重新赋成 1.0，副本反而"优先推荐"用户不想要的东西。
3. ``budget_estimated`` 必须来自落库的报告：写死 True 会让同一份响应里
   ``budget_estimated`` 与 ``feasibility.budget_estimated`` 自相矛盾。
"""

from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal
from typing import Any, cast

import pytest

from app.core.errors import AppError, ErrorCode
from app.db.models import Trip, TripRoute
from app.services import trip_service
from app.services.trip_service import (
    _route_out,
    build_diff,
    plan_request_from_trip,
    undo_last_revision,
)

pytestmark = pytest.mark.unit

#: ``undo_last_revision`` 在"副本是版本链起点"这条路径上**不应**访问数据库，
#: 因此这里刻意传一个不是 AsyncSession 的哨兵：真去查库会立刻炸出来。
_NO_DB: Any = object()


def _snapshot(**overrides: Any) -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        "city": "guangzhou",
        "days": 2,
        "people": 3,
        "pace": "balanced",
        "preferences": {"food": 1.0, "nature": 0.8},
        "budget": {"amount": "500", "scope": "total"},
        "start_time": "10:00",
        "end_time": "19:00",
        "travel_date": "2026-10-01",
    }
    snapshot.update(overrides)
    return snapshot


def _route(report: dict[str, Any], breakdown: dict[str, Any] | None = None) -> TripRoute:
    return TripRoute(
        id=uuid.uuid4(),
        label="A",
        archetype="relaxed",
        name="轻松路线：a → b",
        place_count=3,
        total_duration_min=300,
        recommend_score=0.5,
        score_breakdown=breakdown if breakdown is not None else {},
        feasibility_report=report,
    )


# ── 从意图快照重建规划入参 ──────────────────────────────────────────────────


def test_rebuild_from_snapshot_keeps_only_positive_preferences() -> None:
    snapshot = _snapshot(preferences={"food": 1.0, "photo": 0.0, "nature": 0.8})
    payload = plan_request_from_trip({"copied_from": "x"}, snapshot)
    assert payload.preferences == ["food", "nature"]
    assert "photo" not in payload.preferences, "权重 0 的偏好会在重建时被赋成 1.0，必须丢掉"


def test_rebuild_from_snapshot_handles_string_weights() -> None:
    """jsonb 里的数字可能是字符串（历史数据），按数值判断而不是按真值判断。"""
    payload = plan_request_from_trip({}, _snapshot(preferences={"food": "1.5", "photo": "0"}))
    assert payload.preferences == ["food"]


def test_rebuild_from_snapshot_does_not_replay_original_free_text() -> None:
    """``free_text`` 必须留空：原文重放会把用户这一次的修改覆盖回去。"""
    payload = plan_request_from_trip({}, _snapshot())
    assert payload.free_text == ""
    assert payload.days == 2
    assert payload.people == 3
    assert payload.pace == "balanced"
    assert payload.budget is not None
    assert payload.budget.amount == Decimal("500")
    assert payload.budget.scope == "total"
    assert payload.start_time == "10:00"
    assert payload.end_time == "19:00"
    assert payload.travel_date == "2026-10-01"


def test_rebuild_from_snapshot_keeps_day_span_and_day_plans() -> None:
    """★ 快照里的字段一个都不能漏接 ★ 漏一个，修改/复制一次就把用户的设置洗掉。

    ``day_span`` 与 ``day_plans`` 都曾经在这条路径上被静默丢掉："半天"的行程
    修改一次变回一整天，第 2 天的主题变回"没主题" —— 而新版看起来完全正常。
    """
    snapshot = _snapshot(
        day_span="half_day",
        day_plans=[
            {"day": 1, "pace": "relaxed", "theme": "food"},
            {"day": 2, "pace": "packed", "theme": "culture"},
        ],
    )
    payload = plan_request_from_trip({}, snapshot)

    assert payload.day_span == "half_day"
    assert [(plan.pace, plan.theme) for plan in payload.day_plans] == [
        ("relaxed", "food"),
        ("packed", "culture"),
    ]


def test_rebuild_from_legacy_snapshot_without_day_plans() -> None:
    """旧快照（按天设置之前生成的）没有 ``day_plans``：不能因此报错，
    也不能凭空编出几天的设置 —— 空列表表示"按兜底节奏排"。"""
    payload = plan_request_from_trip({}, _snapshot(day_span="half_day"))

    assert payload.day_plans == []
    assert payload.day_span == "half_day"


def test_raw_input_wins_over_snapshot() -> None:
    raw = {
        "city": "guangzhou",
        "days": 1,
        "people": 2,
        "preferences": ["food"],
        "pace": "relaxed",
        "budget": {"amount": "300", "scope": "per_person"},
        "free_text": "预算 300 元",
    }
    payload = plan_request_from_trip(raw, _snapshot(days=5))
    assert payload.days == 1, "原始表单最可信"
    assert payload.free_text == "预算 300 元"


def test_unparsable_raw_input_falls_back_to_snapshot() -> None:
    """旧数据缺字段时回退到意图快照，而不是把这次修改判为失败。"""
    payload = plan_request_from_trip({"city": "guangzhou", "surprise": 1}, _snapshot(days=4))
    assert payload.days == 4
    assert payload.free_text == ""


# ── 出参诚实性 ──────────────────────────────────────────────────────────────


def test_budget_estimated_follows_stored_report() -> None:
    assert _route_out(_route({"budget_estimated": False}), []).budget_estimated is False
    assert _route_out(_route({"budget_estimated": True}), []).budget_estimated is True


def test_budget_estimated_defaults_to_conservative_when_missing() -> None:
    """报告里没有这个键时保守地标成"含估算值"，而不是声称价格已知。"""
    assert _route_out(_route({}), []).budget_estimated is True


def test_unknown_budget_items_are_exposed() -> None:
    out = _route_out(_route({"budget_estimated": True, "budget_unknown_items": ["长隆门票"]}), [])
    assert out.budget_unknown_items == ["长隆门票"]


def test_estimated_budget_items_are_exposed_separately_from_unknown_ones() -> None:
    """估算项与未知项必须分开往上传。

    两者含义**相反**："没有价格的项（没算进去）" vs "算进去了、但单价是假设"。
    合成一个列表的话，前端只能说一句含糊的"含估算值"；而二者混起来渲染，
    读者会以为那条茶楼餐费没算 ¥ 里 —— 恰好是这次要修的那个错觉。
    """
    out = _route_out(
        _route(
            {
                "budget_estimated": True,
                "budget_unknown_items": ["光明广场·消费"],
                "budget_estimated_items": ["点都德·breakfast"],
            }
        ),
        [],
    )
    assert out.budget_estimated_items == ["点都德·breakfast"]
    assert out.budget_unknown_items == ["光明广场·消费"]
    assert _route_out(_route({}), []).budget_estimated_items == []


# ── Diff ────────────────────────────────────────────────────────────────────


def _signature(names: tuple[str, ...], walking: int, budget: str | None) -> Any:
    return (names, walking, None if budget is None else Decimal(budget))


def test_diff_reports_days_change() -> None:
    """★ "改成 3 天"改变的就是天数，Diff 不能输出一句"什么都没变" ★"""
    diff = build_diff(
        _signature(("a", "b"), 1000, "100"),
        _signature(("a", "b"), 1000, "100"),
        days_before=1,
        days_after=3,
    )
    assert diff.as_dict()["days_before"] == 1
    assert diff.as_dict()["days_after"] == 3
    assert "天数 1 天 → 3 天" in diff.as_dict()["sentence"]


def test_diff_omits_days_when_unchanged() -> None:
    diff = build_diff(
        _signature(("a", "b"), 1000, "100"),
        _signature(("a", "b"), 800, "90"),
        days_before=2,
        days_after=2,
    )
    sentence = diff.as_dict()["sentence"]
    assert "天数" not in sentence
    assert "移除了" not in sentence
    assert diff.as_dict()["walking_delta_m"] == -200


# ── 撤销 ────────────────────────────────────────────────────────────────────


def _fake_load(monkeypatch: pytest.MonkeyPatch, *, source: str) -> list[uuid.UUID]:
    """把 ``load_trip`` 换成可观察的假实现：记录被加载的 id。"""
    loaded: list[uuid.UUID] = []

    async def fake_load(db: Any, trip_id: uuid.UUID) -> Trip:
        loaded.append(trip_id)
        return Trip(id=trip_id, source=source, revision_no=1)

    monkeypatch.setattr(trip_service, "load_trip", fake_load)
    return loaded


@pytest.mark.parametrize("source", ["plan_cache", "copied"])
def test_undo_stops_at_copies_instead_of_walking_into_the_source(
    monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    """★ 副本是版本链的起点 ★

    ``parent_trip_id`` 指向的是**来源**（可能是别人的行程）。沿它往上走会撞上
    归属校验 → 用户拿到 403，而不是"已经是最早的版本"。
    """
    trip = Trip(id=uuid.uuid4(), parent_trip_id=uuid.uuid4(), source=source, revision_no=1)
    loaded = _fake_load(monkeypatch, source="generated")

    with pytest.raises(AppError) as exc:
        asyncio.run(undo_last_revision(cast("Any", _NO_DB), trip))

    assert exc.value.code == ErrorCode.INVALID_INPUT
    assert loaded == [], "不能沿着复制来源继续往上找：那可能是别人的行程"


def test_undo_without_parent_reports_no_history(monkeypatch: pytest.MonkeyPatch) -> None:
    trip = Trip(id=uuid.uuid4(), parent_trip_id=None, source="generated", revision_no=1)
    loaded = _fake_load(monkeypatch, source="generated")

    with pytest.raises(AppError) as exc:
        asyncio.run(undo_last_revision(cast("Any", _NO_DB), trip))

    assert exc.value.code == ErrorCode.INVALID_INPUT
    assert loaded == []


def test_undo_returns_previous_revision(monkeypatch: pytest.MonkeyPatch) -> None:
    parent_id = uuid.uuid4()
    trip = Trip(id=uuid.uuid4(), parent_trip_id=parent_id, source="revision", revision_no=2)
    loaded = _fake_load(monkeypatch, source="generated")

    previous = asyncio.run(undo_last_revision(cast("Any", _NO_DB), trip))

    assert previous.id == parent_id
    assert loaded == [parent_id]
