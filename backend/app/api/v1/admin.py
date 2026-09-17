"""成本后台（PRD FR-12 AC-12.2 / §7.2 ``GET /admin/cost/summary``）。

与 ``/dev`` 面板的关系：``/dev`` 是**开发期**的配置面板（只在 ``ENV=development``
注册，生产上那些路径根本不存在）；成本后台是**产品的一部分**——它在任何环境都可能
被需要，因此不能靠 ``ENV`` 来保护，只能靠 Token。

访问控制与 ``/dev`` 同一条理由、同一套做法，但**少一道门**：

1. **Token 级**：配了 ``ADMIN_TOKEN`` 就必须带 ``X-Admin-Token``，用
   ``hmac.compare_digest`` 比较（防时序侧信道）。
2. **来源级**：**没**配 Token 时只接受本机来源 —— 本地开发者不必为了看一次成本先造一个 Token，
   但"任何人都能从公网读到我们的账单"也不接受。

刻意不做的事：没有登录页、没有用户/权限模型、没有写操作。这个页面只读。
"""

from __future__ import annotations

import hmac
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_limits_config, get_settings
from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger
from app.db.session import get_db
from app.schemas.common import ApiMeta, ok_envelope
from app.services.cost import CostStore

__all__ = ["require_admin", "router"]

log = get_logger("api.admin")

router = APIRouter(tags=["admin"], prefix="/admin")

#: 未配 ADMIN_TOKEN 时允许的来源（与 ``api/v1/dev.py`` 同一份口径）。
_LOCAL_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "testclient"})


async def require_admin(request: Request) -> None:
    """成本后台的访问控制。"""
    settings = get_settings()
    if settings.admin_token:
        provided = request.headers.get("X-Admin-Token", "")
        if not hmac.compare_digest(provided, settings.admin_token):
            raise AppError(
                ErrorCode.ADMIN_REQUIRED,
                "后台令牌不正确",
                hint="在页面顶部填入 .env 里的 ADMIN_TOKEN。",
            )
        return

    host = request.client.host if request.client is not None else None
    if host not in _LOCAL_HOSTS:
        raise AppError(
            ErrorCode.FORBIDDEN,
            "未设置 ADMIN_TOKEN 时只允许本机访问成本后台",
            hint="在 .env 里设置 ADMIN_TOKEN，或从本机（127.0.0.1）打开。",
            context={"host": host or ""},
        )


@router.get("/cost/summary")
async def cost_summary(
    days: int = Query(default=7, ge=1, le=90, description="统计窗口（天）"),
    _: None = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """成本汇总：按类别 / 按 Provider / 单次规划的 P50·P95 / 最贵的几笔。

    ★ 三个如实标注 ★
    1. **未校准的金额与已校准的金额混在一起时，报表要说出来**
       （``pricing_calibrated`` 与 ``uncalibrated_rows``/``uncalibrated_plans``）。
    2. **单次规划成本按 request_id 聚合**，不是拿 ``cost_logs`` 的行平均 ——
       细节见 ``CostStore.plan_cost_stats``。
    3. ``target_cny`` 取的是 ``limits.yaml`` 的 ``warn_plan_cny``（PRD §3.2 的 ≤¥0.5 红线），
       **由配置给出而不是在这里写死**，否则改配置的门槛、页面还报着旧数字。
    """
    limits = get_limits_config()
    store = CostStore(db)
    summary = await store.summary(days=days)
    by_provider = await store.by_provider(days=days)
    plans = await store.plan_cost_stats(days=days)
    daily = await store.daily_budget(limit_cny=limits.cost.global_daily_cny)

    calls = sum(int(row["calls"]) for row in summary["by_category"])
    hits = sum(int(row["cache_hits"]) for row in summary["by_category"])
    target = float(limits.cost.warn_plan_cny)
    payload = {
        "window_days": days,
        "by_category": summary["by_category"],
        "by_provider": by_provider,
        "plans": {
            **plans,
            "target_cny": target,
            # 达标是**结论**，由后端算一次：让每个消费方各算一遍，迟早会有一处口径不同。
            # 未校准的窗口不算达标 —— 金额本身不可信时，"低于红线"是一句没有根据的话。
            "within_target": (
                None
                if plans["p95_cny"] is None or plans["uncalibrated_plans"] > 0
                else plans["p95_cny"] <= target
            ),
        },
        "cache": {
            "calls": calls,
            "hits": hits,
            "hit_rate": None if calls == 0 else round(hits / calls, 4),
        },
        "pricing_calibrated": summary["pricing_calibrated"],
        "uncalibrated_rows": summary["uncalibrated_rows"],
        "note": summary["note"],
        "daily": {
            "spent_cny": str(daily.spent_cny),
            "limit_cny": daily.limit_cny,
            "exceeded": daily.exceeded,
        },
        "breakers": {
            "plan_total_cny": limits.cost.circuit_breaker.plan_total_cny,
            "plan_search_cny": limits.cost.circuit_breaker.plan_search_cny,
            "plan_map_calls": limits.cost.circuit_breaker.plan_map_calls,
            "plan_llm_calls": limits.cost.circuit_breaker.plan_llm_calls,
        },
    }
    log.info(
        "读取成本汇总",
        extra={"event": "admin.cost_summary", "context": {"days": days}},
    )
    return ok_envelope(payload, ApiMeta())
