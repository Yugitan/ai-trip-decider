"""缓存三件套（PRD §15.3）。

层次：**进程内 LRU（60s 热点）→ Postgres 表（持久）**。Redis 属 `[P1]`。

★ 键设计里的两个"自然失效"机关 ★
- ``params_hash`` 含知识库版本由 ``plan_cache.kb_version`` 承担；
  数据更新后旧缓存自动失效，而不是靠人工清理。
- ``llm_cache_key`` 含 ``prompt_version`` 与 ``schema_hash``：
  改 prompt 或改 schema 后旧缓存自动失效，避免"新逻辑读到旧模型输出"。

所有键构造都是**纯函数**（可脱离数据库单测）；只有 ``CacheStore`` 触库。
"""

from __future__ import annotations

import hashlib
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import select, update

# PostgreSQL 方言的 insert 才有 on_conflict_do_update（通用 insert 没有）。
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import LlmCache, PlanCache, SearchCache

__all__ = [
    "CacheStore",
    "CacheValue",
    "MemoryCache",
    "llm_cache_key",
    "params_hash",
    "search_cache_key",
    "sha256_hex",
]

CacheLayer = Literal["memory", "db"]


@dataclass(frozen=True, slots=True)
class CacheValue[T]:
    value: T
    layer: CacheLayer


# ════════════════════════════════════════════════════════════════════════════
# 一、键构造（纯函数）
# ════════════════════════════════════════════════════════════════════════════


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def params_hash(
    *,
    city: str,
    days: int,
    people: int,
    preferences: Sequence[str],
    pace: str,
    budget_scope: str,
    budget_amount: Decimal | float | None,
    start_time: str,
    end_time: str,
    travel_date: str | None = None,
    exclusions: Sequence[str] = (),
    constraints: Sequence[str] = (),
) -> str:
    """规划参数指纹（PRD §15.3）。

    列表一律**排序后**入键：``[food, photo]`` 与 ``[photo, food]`` 必须命中同一条缓存，
    否则用户改一下偏好勾选顺序就会多花一次冷启动的钱。
    金额用 ``str`` 规范化，避免 ``300`` 与 ``300.00`` 生成两个键。
    """
    canonical = [
        city.strip().lower(),
        str(int(days)),
        str(int(people)),
        "|".join(sorted(p.strip().lower() for p in preferences)),
        pace,
        budget_scope,
        "" if budget_amount is None else str(Decimal(str(budget_amount)).normalize()),
        start_time,
        end_time,
        travel_date or "",
        "|".join(sorted(e.strip().lower() for e in exclusions)),
        "|".join(sorted(c.strip().lower() for c in constraints)),
    ]
    return sha256_hex("\x1f".join(canonical))


def llm_cache_key(
    *,
    tier: str,
    model: str,
    prompt_version: str,
    prompt_text: str,
    schema_hash: str,
    temperature: float,
) -> str:
    return sha256_hex(
        "\x1f".join(
            [tier, model, prompt_version, prompt_text, schema_hash, f"{temperature:.3f}"]
        )
    )


def search_cache_key(
    *,
    provider: str,
    query: str,
    locale: str = "zh-CN",
    max_results: int = 8,
    recency_days: int | None = None,
) -> str:
    # 查询文本归一化：去首尾空白 + 折叠内部空白，避免"广州 美食"与"广州  美食"各存一份
    normalized = " ".join(query.split()).lower()
    return sha256_hex(
        "\x1f".join(
            [provider, normalized, locale, str(int(max_results)), "" if recency_days is None else str(int(recency_days))]
        )
    )


# ════════════════════════════════════════════════════════════════════════════
# 二、进程内 TTL + LRU（第一层缓存）
# ════════════════════════════════════════════════════════════════════════════


@dataclass
class _MemoryEntry[T]:
    value: T
    expires_at: float


@dataclass
class MemoryCache[T]:
    """带 TTL 与 LRU 淘汰的进程内缓存。

    为什么需要它：Postgres 缓存每次命中也要一次往返（~几毫秒），
    而同一规划里同一个键常被反复查（例如同一个 LLM prompt 在多套方案中复用）。
    60 秒的内存层把这类热点挡在数据库之外，且天然不会读到"上一轮部署的旧数据"。

    ``clock`` 可注入，便于测试过期行为而不用真的 sleep。
    """

    ttl_s: float = 60.0
    max_entries: int = 256
    clock: Callable[[], float] = time.monotonic
    _entries: OrderedDict[str, _MemoryEntry[T]] = field(
        default_factory=OrderedDict, init=False
    )
    hits: int = field(default=0, init=False)
    misses: int = field(default=0, init=False)
    evictions: int = field(default=0, init=False)

    def get(self, key: str) -> T | None:
        entry = self._entries.get(key)
        if entry is None:
            self.misses += 1
            return None
        if entry.expires_at <= self.clock():
            del self._entries[key]
            self.misses += 1
            return None
        self._entries.move_to_end(key)
        self.hits += 1
        return entry.value

    def set(self, key: str, value: T) -> None:
        if self.max_entries <= 0:
            return
        self._entries[key] = _MemoryEntry(value=value, expires_at=self.clock() + self.ttl_s)
        self._entries.move_to_end(key)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)
            self.evictions += 1

    def invalidate(self, key: str) -> None:
        self._entries.pop(key, None)

    def clear(self) -> None:
        self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)


# ════════════════════════════════════════════════════════════════════════════
# 三、Postgres 持久缓存（第二层）
# ════════════════════════════════════════════════════════════════════════════


@dataclass
class CacheStore:
    """``search_cache`` / ``llm_cache`` / ``plan_cache`` 的读写。

    命中时会把 ``hit_count`` 加一并记 ``last_hit_at`` —— 缓存命中率是 PRD 的核心成本指标，
    不记录就没法优化。
    """

    session: AsyncSession

    # ── 搜索缓存 ──

    async def get_search(self, cache_key: str) -> dict[str, Any] | None:
        row = (
            await self.session.execute(select(SearchCache).where(SearchCache.cache_key == cache_key))
        ).scalar_one_or_none()
        if row is None:
            return None
        if _is_expired(row.expires_at):
            return None
        row.hit_count += 1
        row.last_hit_at = datetime.now(UTC)
        return dict(row.result)

    async def put_search(
        self,
        *,
        cache_key: str,
        provider: str,
        query: str,
        result: dict[str, Any],
        ttl_hours: int,
        locale: str = "zh-CN",
        extracted: dict[str, Any] | None = None,
        cost_cny: Decimal = Decimal("0"),
    ) -> None:
        self.session.add(
            SearchCache(
                cache_key=cache_key,
                provider=provider,
                query=query,
                locale=locale,
                result=result,
                extracted=extracted,
                cost_cny=cost_cny,
                expires_at=_expiry(ttl_hours),
            )
        )
        await self.session.flush()

    # ── LLM 缓存 ──

    async def get_llm(self, cache_key: str) -> dict[str, Any] | None:
        row = (
            await self.session.execute(select(LlmCache).where(LlmCache.cache_key == cache_key))
        ).scalar_one_or_none()
        if row is None:
            return None
        if _is_expired(row.expires_at):
            return None
        row.hit_count += 1
        row.last_hit_at = datetime.now(UTC)
        return dict(row.response)

    async def put_llm(
        self,
        *,
        cache_key: str,
        tier: str,
        model: str,
        prompt_hash: str,
        task: str,
        response: dict[str, Any],
        ttl_hours: int | None,
        tokens_in: int = 0,
        tokens_out: int = 0,
        tokens_cached: int = 0,
        cost_cny: Decimal = Decimal("0"),
    ) -> None:
        # ttl_hours=None 表示"不过期"（结构化的 LLM 输出靠 prompt 版本失效，
        # 见 config/ttl.yaml 的 llm_structured）。这里用一个极远的过期时间表达。
        #
        # ★ 先查再插，而不是硬插 ★
        # ``cache_key`` 唯一；两个并发规划算出同一个 prompt 时，后写的一方
        # 会撞唯一约束。失败的 flush 会把整个会话打成 PendingRollback ——
        # 上层 ``except`` 吞得掉异常，吞不掉这个状态，本次规划随后必然 500。
        # 所以撞键时保留已有行（先到先得，两份内容等价：同一个键就是同一个
        # prompt + 同一个 schema），并刷新命中时间。先用 SELECT 查一遍而不是
        # 硬插后捕 IntegrityError：后者同样会污染当前事务。
        existing = (
            await self.session.execute(
                select(LlmCache).where(LlmCache.cache_key == cache_key)
            )
        ).scalar_one_or_none()
        expires_at = _expiry(ttl_hours) if ttl_hours is not None else _far_future()
        if existing is not None:
            existing.last_hit_at = datetime.now(UTC)
            return
        self.session.add(
            LlmCache(
                cache_key=cache_key,
                tier=tier,
                model=model,
                prompt_hash=prompt_hash,
                task=task,
                response=response,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                tokens_cached=tokens_cached,
                cost_cny=cost_cny,
                expires_at=expires_at,
            )
        )
        await self.session.flush()

    # ── 规划结果缓存 ──

    async def get_plan(self, params_hash_value: str, kb_version: str) -> uuid.UUID | None:
        row = (
            await self.session.execute(
                select(PlanCache).where(
                    PlanCache.params_hash == params_hash_value,
                    PlanCache.kb_version == kb_version,
                )
            )
        ).scalar_one_or_none()
        if row is None or _is_expired(row.expires_at):
            return None
        await self.session.execute(
            update(PlanCache)
            .where(PlanCache.id == row.id)
            .values(hit_count=PlanCache.hit_count + 1)
        )
        return row.trip_id

    async def put_plan(
        self,
        *,
        params_hash_value: str,
        kb_version: str,
        trip_id: uuid.UUID,
        ttl_hours: int,
    ) -> None:
        """写入规划缓存（**幂等**）。

        ★ 必须 upsort 而不是裸 INSERT ★
        ``(params_hash, kb_version)`` 上有唯一约束，而**同一个键出现两次是完全正常的**：
        修改（revise）刻意绕过缓存，若改出来的参数与之前某次相同，就会插第二条；
        两个会话同时提交同一组参数也会。裸 INSERT 会直接撞唯一约束 → 500，
        而用户看到的是一个"明明很正常的请求失败了"。冲突时更新 trip_id 与过期时间：
        同一组参数下最新算出的方案同样有效，且顺带续期。
        """
        statement = (
            insert(PlanCache)
            .values(
                params_hash=params_hash_value,
                kb_version=kb_version,
                trip_id=trip_id,
                expires_at=_expiry(ttl_hours),
            )
            .on_conflict_do_update(
                index_elements=[PlanCache.params_hash, PlanCache.kb_version],
                set_={"trip_id": trip_id, "expires_at": _expiry(ttl_hours)},
            )
        )
        await self.session.execute(statement)
        await self.session.flush()


def _expiry(ttl_hours: int) -> datetime:
    return datetime.now(UTC) + timedelta(hours=ttl_hours)


def _far_future() -> datetime:
    return datetime.now(UTC) + timedelta(days=3650)


def _is_expired(expires_at: datetime) -> bool:
    """过期判断统一走这里。

    ``expires_at`` 在数据库里是 timestamptz，读回来是**带时区**的 datetime；
    但也有可能是 naive（例如测试里手工构造）。naive 一律按 UTC 解释，
    避免 ``TypeError: can't subtract offset-naive and offset-aware datetimes``。
    """
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return expires_at <= datetime.now(UTC)
