"""联网搜索管线的单元测试（纯函数，零网络、零数据库）。

这里守的是 PRD §13.4 的**成本核心**：什么时候不该搜。
写法上刻意把"该搜"与"不该搜"两条都钉住 —— 只测"该搜"会让决策树退化成
"永远搜"，而那是这个系统里最贵的一种 bug（每个请求都花钱）。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import cast

import pytest

from app.core.config import get_limits_config, get_scoring_config, get_ttl_config
from app.db.models import Place
from app.domain.models import Intent
from app.providers.search.base import SearchResult
from app.services.llm_planner import LlmSearchObservation, SearchFactField
from app.services.search_pipeline import (
    SearchEvidence,
    SearchTrigger,
    _as_results,
    _Candidate,
    _conflicts_with_local,
    _match_place,
    _search_budget,
    decide_triggers,
    plan_queries,
    preference_queries,
)

pytestmark = pytest.mark.unit

_LIMITS = get_limits_config()
_TTL = get_ttl_config()
_SCORING = get_scoring_config()


def _intent(**overrides: object) -> Intent:
    return Intent(**{"start_min": 9 * 60, "end_min": 21 * 60, **overrides})  # type: ignore[arg-type]


def _candidate(
    *, has_hours: bool = True, hours_stale: bool = False, conflicting: bool = False
) -> _Candidate:
    return _Candidate(
        name="广州塔",
        has_hours=has_hours,
        hours_stale=hours_stale,
        conflicting=conflicting,
    )


def _decide(**overrides: object) -> tuple[SearchTrigger, ...]:
    params: dict[str, object] = {
        "city_name": "广州",
        "intent": _intent(days=2, preferences={"food": 1.0, "photo": 0.5}),
        "free_text": "",
        "unknown_names": (),
        "candidate_count": 25,
        "candidates": (_candidate(),),
        "limits": _LIMITS,
        "ttl": _TTL,
        "budget_ready": True,
    }
    params.update(overrides)
    return decide_triggers(**params)  # type: ignore[arg-type]


def _kinds(triggers: tuple[SearchTrigger, ...]) -> list[str]:
    return [trigger.kind for trigger in triggers]


# ── 决策树：不该搜的路径 ────────────────────────────────────────────────────


def test_no_trigger_on_the_default_path() -> None:
    """★ 最重要的一条 ★ 库够用、没冲突、没提新东西 → **一次都不搜**（≈95% 的请求）。"""
    assert _decide() == ()


def test_no_trigger_without_a_usable_provider() -> None:
    """没有可用 Provider 时连触发列表都不产出：不能"记为搜过了"却什么都没发。"""
    triggers = _decide(free_text="想找最近新开的餐厅", budget_ready=False)
    assert triggers == ()


# ── 决策树：五条该搜的分支 ──────────────────────────────────────────────────


def test_conflict_arbitration_comes_first_and_is_capped_at_one() -> None:
    triggers = _decide(candidates=(_candidate(conflicting=True), _candidate(conflicting=True)))
    assert _kinds(triggers)[0] == "arbitrate_conflict"
    assert len(triggers[0].queries) == 1  # search_budget.arbitrate_conflict = 1


def test_stale_hard_constraint_verification() -> None:
    triggers = _decide(candidates=(_candidate(hours_stale=True),))
    assert _kinds(triggers) == ["verify_hard_constraint"]
    assert triggers[0].queries == ("广州塔 开放时间",)


def test_unknown_place_gets_entity_queries_capped_at_two() -> None:
    triggers = _decide(unknown_names=("永庆坊二期", "海心沙", "第三个"))
    assert _kinds(triggers) == ["user_named_unknown_place"]
    assert len(triggers[0].queries) == 2  # search_budget.user_named_unknown_place = 2


def test_candidate_shortage_triggers_discovery_with_anchor_queries() -> None:
    triggers = _decide(candidate_count=3)
    assert _kinds(triggers) == ["discover_more_candidates"]
    assert triggers[0].queries[0] == "广州 2日游 路线"
    assert len(triggers[0].queries) <= 3  # search_budget.discover_more_candidates = 3


def test_recency_request_adds_a_month_query() -> None:
    triggers = _decide(free_text="想看看最近有没有夜游珠江的新玩法")
    assert _kinds(triggers) == ["user_requested_latest"]
    joined = " ".join(triggers[0].queries)
    assert "活动 展览" in joined


def test_trigger_order_follows_the_decision_tree() -> None:
    triggers = _decide(
        free_text="最近有什么新开的展",
        unknown_names=("海心沙",),
        candidate_count=2,
        candidates=(_candidate(hours_stale=True, conflicting=True),),
    )
    assert _kinds(triggers) == [
        "arbitrate_conflict",
        "verify_hard_constraint",
        "user_named_unknown_place",
        "discover_more_candidates",
        "user_requested_latest",
    ]


def test_plan_queries_dedupes_and_respects_the_hard_cap() -> None:
    triggers = _decide(free_text="最近有什么新玩法", candidate_count=0)
    queries = plan_queries(triggers, hard_cap=3)
    assert len(queries) == 3
    assert len(set(queries)) == 3
    # "发现新地点"与"时效查询"共用锚定查询，去重后不应该出现两次
    assert queries.count("广州 2日游 路线") == 1


def test_plan_queries_with_zero_cap_searches_nothing() -> None:
    triggers = _decide(free_text="最新")
    assert plan_queries(triggers, hard_cap=0) == ()


# ── 查询生成 ────────────────────────────────────────────────────────────────


def test_preference_queries_map_dimensions_and_cap_at_three() -> None:
    intent = _intent(preferences={"food": 1.0, "photo": 1.0, "night_view": 1.0, "nature": 1.0})
    queries = preference_queries(intent)
    assert len(queries) == 3
    assert "美食 推荐" in queries


def test_preference_queries_ignore_unknown_and_zero_weight_dimensions() -> None:
    assert preference_queries(_intent(preferences={"not_a_dimension": 1.0})) == ()


# ── 预算读取 ────────────────────────────────────────────────────────────────


def test_search_budget_reads_every_trigger_from_config() -> None:
    budget = _search_budget(_TTL)
    assert budget["arbitrate_conflict"] == 1
    assert budget["discover_more_candidates"] == 3
    assert set(budget) == {
        "verify_hard_constraint",
        "user_named_unknown_place",
        "discover_more_candidates",
        "user_requested_latest",
        "arbitrate_conflict",
    }


# ── 结果解析（活的 SearchResult / 缓存里的 dict）──────────────────────────────


def test_as_results_accepts_live_results_and_cached_dicts() -> None:
    live = [SearchResult(title="t", url="https://a.com/1", snippet="s", domain="a.com")]
    assert _as_results(live)[0].url == "https://a.com/1"
    cached = {"query": "q", "results": [{"title": "t", "url": "https://a.com/1", "snippet": "s"}]}
    assert _as_results(cached)[0].title == "t"


def test_as_results_rejects_junk_without_raising() -> None:
    assert _as_results(None) == ()
    assert _as_results("https://a.com") == ()
    assert _as_results([{"title": "没有 url"}]) == ()
    assert _as_results({"results": None}) == ()


# ── 实体匹配（PRD §14）──────────────────────────────────────────────────────


def _place(name: str) -> Place:
    return Place(
        id=uuid.uuid4(),
        city_id=uuid.uuid4(),
        canonical_name=name,
        display_name=name,
        category="attraction",
        latitude=23.1,
        longitude=113.3,
    )


def test_match_place_exact_and_alias() -> None:
    guangzhou_tower = _place("广州塔")
    index = {"广州塔": guangzhou_tower}
    aliases = {"小蛮腰": guangzhou_tower}
    assert _match_place("广州塔", index, aliases=aliases, floor=0.9) is guangzhou_tower
    assert _match_place("小蛮腰", index, aliases=aliases, floor=0.9) is guangzhou_tower


def test_match_place_refuses_low_similarity() -> None:
    """不相近的名字必须是"库外线索"，不能被强行归到某个地点上。"""
    index = {"广州塔": _place("广州塔")}
    assert _match_place("深圳湾公园", index, aliases={}, floor=0.9) is None


def test_match_place_never_matches_empty_name() -> None:
    index = {"广州塔": _place("广州塔")}
    assert _match_place("   ", index, aliases={}, floor=0.5) is None


# ── 冲突判定 ────────────────────────────────────────────────────────────────


def _observation(field: str, value: str) -> LlmSearchObservation:
    return LlmSearchObservation(
        place_name="广州塔",
        field=cast(SearchFactField, field),
        value=value,
        url="https://a.com/1",
    )


def test_conflict_requires_a_local_value_to_contradict() -> None:
    """★ unknown ≠ conflicting ★ 库里没有值的时候，搜索结果只能算补充线索。"""
    place = _place("广州塔")
    assert _conflicts_with_local(place, _observation("opening_hours", "9:00-17:30")) is False


def test_conflict_detects_a_different_value() -> None:
    place = _place("广州塔")
    place.opening_hours_raw = "9:30-22:00"
    assert _conflicts_with_local(place, _observation("opening_hours", "9:00-17:30")) is True


def test_conflict_ignores_formatting_and_containment() -> None:
    place = _place("广州塔")
    place.opening_hours_raw = "09:30-22:00"
    assert _conflicts_with_local(place, _observation("opening_hours", " 09:30-22:00 ")) is False
    assert _conflicts_with_local(place, _observation("opening_hours", "09:30-22:00（周一场馆维护）")) is False


def test_conflict_on_price_uses_the_local_price_note() -> None:
    place = _place("广州塔")
    place.price_note = "150 元"
    assert _conflicts_with_local(place, _observation("price", "免费")) is True
    assert _conflicts_with_local(place, _observation("price", "150 元")) is False


def test_conflict_never_fires_for_note_or_event_fields() -> None:
    place = _place("广州塔")
    place.opening_hours_raw = "9:30-22:00"
    assert _conflicts_with_local(place, _observation("note", "本周有灯光秀")) is False
    assert _conflicts_with_local(place, _observation("event", "中秋灯会")) is False


# ── 摘要 ────────────────────────────────────────────────────────────────────


def test_evidence_summary_reports_no_search_on_the_default_path() -> None:
    assert SearchEvidence().summary() == {"used": False}


def test_evidence_summary_lists_triggers_when_searched() -> None:
    triggers = _decide(free_text="最近新开的店")
    evidence = SearchEvidence(triggers=triggers, queries=("q",), calls=1, provider="tavily")
    summary = evidence.summary()
    assert summary["used"] is True
    assert summary["triggers"] == ["user_requested_latest"]


def test_evidence_cost_defaults_to_zero() -> None:
    assert SearchEvidence().cost_cny == Decimal("0")


def test_scoring_config_exposes_domain_credibility() -> None:
    """配置里的域名可信度必须真的能被读到（否则 §13.3 的加权就是空谈）。"""
    assert _SCORING.search_source_credibility.score_for("www.gz.gov.cn") > 0.9
    assert _SCORING.search_source_credibility.score_for("未知域名.invalid") == 0.5


def test_travel_date_proximity_gate_is_configured() -> None:
    window = _TTL.refresh_policy.get("travel_date_proximity_days")
    assert isinstance(window, int) and window >= 1


def test_stale_hours_branch_uses_place_opening_hours_ttl() -> None:
    ttl_hours = _TTL.ttl_for("place_opening_hours")
    assert ttl_hours is not None
    now = datetime.now(UTC)
    fresh = now - timedelta(hours=ttl_hours - 1)
    stale = now - timedelta(hours=ttl_hours + 1)
    assert fresh < now and stale < fresh
