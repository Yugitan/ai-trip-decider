"""缓存层单元测试（PRD §15.3）。

守两件事：
1. **键构造的稳定性**：偏好勾选顺序、金额写法（300 vs 300.00）、查询里的多余空格
   都不能生成不同的键 —— 否则缓存命中率会被"无关改动"悄悄拉低，直接影响成本红线。
2. **进程内缓存的行为**：TTL 过期、LRU 淘汰、命/未命中计数。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from app.services.cache import (
    MemoryCache,
    _is_expired,
    llm_cache_key,
    params_hash,
    search_cache_key,
    sha256_hex,
)

pytestmark = pytest.mark.unit


# ── 键构造 ──────────────────────────────────────────────────────────────────


def _params(**overrides: object) -> str:
    base: dict[str, object] = {
        "city": "guangzhou",
        "days": 1,
        "people": 2,
        "preferences": ["food", "photo"],
        "pace": "relaxed",
        "budget_scope": "per_person",
        "budget_amount": Decimal("300"),
        "start_time": "09:00",
        "end_time": "21:00",
    }
    base.update(overrides)
    return params_hash(**base)  # type: ignore[arg-type]


def test_sha256_hex_is_deterministic_and_hex() -> None:
    assert sha256_hex("广州") == sha256_hex("广州")
    assert len(sha256_hex("广州")) == 64
    assert sha256_hex("广州") != sha256_hex("深圳")


def test_params_hash_ignores_preference_order() -> None:
    """勾选顺序不同必须命中同一条缓存，否则用户点两下就多花一次冷启动的钱。"""
    assert _params(preferences=["food", "photo"]) == _params(preferences=["photo", "food"])


def test_params_hash_ignores_amount_representation() -> None:
    assert _params(budget_amount=Decimal("300")) == _params(budget_amount=300.0)


def test_params_hash_normalizes_whitespace_and_case() -> None:
    assert _params(city=" Guangzhou ") == _params(city="guangzhou")


def test_params_hash_is_sensitive_to_real_changes() -> None:
    assert _params(days=1) != _params(days=2)
    assert _params(pace="relaxed") != _params(pace="packed")
    assert _params(budget_amount=None) != _params(budget_amount=Decimal("300"))


def test_params_hash_accepts_none_amount_as_unlimited() -> None:
    assert _params(budget_amount=None) == _params(budget_amount=None)


def test_params_hash_exclusions_and_constraints_are_order_insensitive() -> None:
    a = _params(exclusions=["广州塔", "白天鹅宾馆"], constraints=["max_walking"])
    b = _params(exclusions=["白天鹅宾馆", "广州塔"], constraints=["max_walking"])
    assert a == b


def test_params_hash_travel_date_changes_key() -> None:
    assert _params(travel_date=None) != _params(travel_date="2026-10-01")


def test_llm_cache_key_changes_with_prompt_version() -> None:
    """改 prompt 必须让旧缓存失效，否则会读到"用旧规则生成的新数据"。"""
    base: dict[str, Any] = {
        "tier": "fast",
        "model": "deepseek-chat",
        "prompt_version": "v1",
        "prompt_text": "解析意图",
        "schema_hash": "abc",
        "temperature": 0.0,
    }
    assert llm_cache_key(**base) != llm_cache_key(**{**base, "prompt_version": "v2"})
    assert llm_cache_key(**base) != llm_cache_key(**{**base, "schema_hash": "xyz"})
    assert llm_cache_key(**base) == llm_cache_key(**base)


def test_search_cache_key_normalizes_query_whitespace() -> None:
    a = search_cache_key(provider="tavily", query="广州 美食")
    b = search_cache_key(provider="tavily", query="  广州   美食 ")
    assert a == b


def test_search_cache_key_distinguishes_recency() -> None:
    assert search_cache_key(provider="tavily", query="x", recency_days=None) != search_cache_key(
        provider="tavily", query="x", recency_days=7
    )


def test_search_cache_key_distinguishes_provider_and_max_results() -> None:
    assert search_cache_key(provider="tavily", query="x") != search_cache_key(
        provider="serper", query="x"
    )
    assert search_cache_key(provider="tavily", query="x", max_results=8) != search_cache_key(
        provider="tavily", query="x", max_results=3
    )


# ── 进程内缓存 ──────────────────────────────────────────────────────────────


class _Clock:
    """可推进的假时钟：让 TTL 测试瞬间完成，不依赖 sleep。"""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_memory_cache_hit_and_miss() -> None:
    cache: MemoryCache[str] = MemoryCache(ttl_s=60, max_entries=4, clock=_Clock())
    assert cache.get("missing") is None
    cache.set("a", "A")
    assert cache.get("a") == "A"
    assert cache.hits == 1
    assert cache.misses == 1


def test_memory_cache_expires_after_ttl() -> None:
    clock = _Clock()
    cache: MemoryCache[str] = MemoryCache(ttl_s=60, max_entries=4, clock=clock)
    cache.set("a", "A")
    clock.advance(61)
    assert cache.get("a") is None
    assert len(cache) == 0, "过期项必须被删除，否则内存会缓慢泄漏"


def test_memory_cache_lru_eviction() -> None:
    cache: MemoryCache[int] = MemoryCache(ttl_s=60, max_entries=2, clock=_Clock())
    cache.set("a", 1)
    cache.set("b", 2)
    cache.get("a")  # 访问 a → a 变为最近使用
    cache.set("c", 3)  # 淘汰最久未使用的 b
    assert cache.get("a") == 1
    assert cache.get("b") is None
    assert cache.get("c") == 3
    assert cache.evictions == 1


def test_memory_cache_zero_capacity_disables_storage() -> None:
    """max_entries=0 表示"不缓存"而不是"无限缓存"（避免误配置把内存吃光）。"""
    cache: MemoryCache[str] = MemoryCache(ttl_s=60, max_entries=0, clock=_Clock())
    cache.set("a", "A")
    assert cache.get("a") is None
    assert len(cache) == 0


def test_memory_cache_invalidate_and_clear() -> None:
    cache: MemoryCache[str] = MemoryCache(ttl_s=60, max_entries=4, clock=_Clock())
    cache.set("a", "A")
    cache.set("b", "B")
    cache.invalidate("a")
    assert cache.get("a") is None
    cache.clear()
    assert len(cache) == 0


def test_is_expired_treats_naive_datetime_as_utc() -> None:
    """数据库读回的 timestamptz 带时区，但手工构造的可能是 naive —— 不能因此崩掉。"""
    naive_past = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=1)
    naive_future = datetime.now(UTC).replace(tzinfo=None) + timedelta(seconds=1)
    assert _is_expired(naive_past) is True
    assert _is_expired(naive_future) is False
    assert _is_expired(datetime.now(UTC) - timedelta(seconds=1)) is True


def test_memory_cache_overwrite_refreshes_ttl() -> None:
    clock = _Clock()
    cache: MemoryCache[str] = MemoryCache(ttl_s=60, max_entries=4, clock=clock)
    cache.set("a", "A")
    clock.advance(50)
    cache.set("a", "A2")
    clock.advance(50)  # 距最后一次写入 50s < 60s
    assert cache.get("a") == "A2"
