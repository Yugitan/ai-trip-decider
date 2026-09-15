"""限流（PRD §15.4、§24）。

用 ``rate_limit_counters`` 表做**固定窗口**计数：多实例部署时天然一致，
不需要 Redis（MVP 不引 Redis，见 §20.1 后台任务选型）。

窗口对齐到整点边界（``floor(now / window) * window``），因此"重试时间"是确定的、
可展示给用户的秒数，而不是"随便等一会儿"。缺点（窗口边界处可能出现 2 倍突发）
对"防刷量"场景可接受 —— 我们不是在做精确的流量整形。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.faults import fault_active
from app.db.models import RateLimitCounter

__all__ = ["RateLimitResult", "RateLimiter"]


@dataclass(frozen=True, slots=True)
class RateLimitResult:
    allowed: bool
    used: int
    limit: int
    retry_after_s: int

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)


@dataclass
class RateLimiter:
    session: AsyncSession

    async def hit(self, key: str, *, limit: int, window_s: int) -> RateLimitResult:
        """记一次请求并返回是否放行。

        ``limit <= 0`` 视为"不限制"（配置里把某项设为 0 表示关闭该限制），
        返回 ``allowed=True`` 且不写库 —— 让"关闭限流"不需要改代码。

        ``FAULT_INJECTION=rate_limit`` 时**一律拒绝**（PRD §23.5），用来验证 429
        与那句人话提示真的能到达用户。刻意排在 ``limit <= 0`` **之后**：
        "显式关掉限流"是配置（人说的），比故障注入更优先。
        """
        if limit <= 0:
            return RateLimitResult(allowed=True, used=0, limit=0, retry_after_s=0)
        if fault_active("rate_limit"):
            return RateLimitResult(allowed=False, used=limit + 1, limit=limit, retry_after_s=60)

        now = datetime.now(UTC)
        window_start = _window_start(now, window_s)
        statement = (
            insert(RateLimitCounter)
            .values(key=key, window_start=window_start, count=1)
            .on_conflict_do_update(
                index_elements=[RateLimitCounter.key, RateLimitCounter.window_start],
                set_={"count": RateLimitCounter.count + 1},
            )
            .returning(RateLimitCounter.count)
        )
        used = int((await self.session.execute(statement)).scalar_one())
        retry_after = max(0, int((window_start + timedelta(seconds=window_s) - now).total_seconds()))
        return RateLimitResult(allowed=used <= limit, used=used, limit=limit, retry_after_s=retry_after)


def _window_start(now: datetime, window_s: int) -> datetime:
    epoch = int(now.timestamp())
    return datetime.fromtimestamp(epoch - (epoch % window_s), tz=UTC)
