"""健康检查与降级状态。

`/health`       —— 永远返回 200，如实报告各能力是否处于降级模式（供 UI 与运维查看）
`/health/ready` —— 依赖不可用时返回 503（供编排系统判断是否可以接流量）
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession

from app import __version__
from app.core.config import (
    get_limits_config,
    get_pricing_config,
    get_scoring_config,
    get_settings,
    get_ttl_config,
)
from app.db.session import get_db
from app.services.cost import CostStore

router = APIRouter(tags=["health"])


async def _db_probe(db: AsyncSession) -> dict[str, object]:
    """探测数据库连通性与知识库规模。表不存在时如实报告，而不是假装健康。"""
    started = time.perf_counter()
    try:
        await db.execute(sql_text("SELECT 1"))
    except Exception as exc:
        return {"ok": False, "error": type(exc).__name__, "latency_ms": None}

    latency_ms = int((time.perf_counter() - started) * 1000)
    info: dict[str, object] = {"ok": True, "latency_ms": latency_ms}
    try:
        places = (await db.execute(sql_text("SELECT count(*) FROM places WHERE status = 'active'"))).scalar_one()
        routes = (await db.execute(sql_text("SELECT count(*) FROM routes"))).scalar_one()
        cites = (await db.execute(sql_text("SELECT count(*) FROM cities"))).scalar_one()
        info.update({"active_places": int(places), "routes": int(routes), "cities": int(cites)})
        kb = (await db.execute(sql_text("SELECT version FROM kb_versions ORDER BY created_at DESC LIMIT 1"))).scalar()
        info["kb_version"] = kb
        info["schema_ready"] = True
    except Exception:
        info["schema_ready"] = False
    return info


async def _cost_probe(db: AsyncSession) -> dict[str, object]:
    """今天的累计支出与「全局日成本」额度状态（PRD §15.4 第四级熔断）。

    ★ 为什么要报它 ★
    `config/limits.yaml` 的 `cost.global_daily_cny` 曾经**没有任何代码读它** ——
    一个拦不住支出的阈值比没有更危险：看到它会以为已经有兜底。
    现在规划链会据此进「缓存优先模式」，而这里把那件事如实报出来。

    库连不上时 `budget_exceeded` 返回 **None（不知道）**而不是 False（没超）——
    与本项目一贯的「null ≠ 0」同一条规矩。
    """
    limit_cny = get_limits_config().cost.global_daily_cny
    try:
        budget = await CostStore(db).daily_budget(limit_cny=limit_cny)
    except Exception as exc:
        return {
            "daily_spend_cny": None,
            "global_daily_budget_cny": limit_cny,
            "budget_exceeded": None,
            "unavailable": type(exc).__name__,
        }
    return {
        "daily_spend_cny": str(budget.spent_cny),
        "global_daily_budget_cny": budget.limit_cny,
        "budget_exceeded": budget.exceeded,
        "disabled_by_config": budget.off,
    }


def _payload(db_info: dict[str, object], cost_info: dict[str, object]) -> dict[str, object]:
    settings = get_settings()
    pricing = get_pricing_config()
    schema_state = "ok" if db_info.get("schema_ready") else "migration_pending"
    return {
        "status": "ok" if db_info.get("ok") and db_info.get("schema_ready") else "degraded",
        "version": __version__,
        "env": settings.env,
        "database": db_info,
        "schema_state": schema_state,
        "degraded_modes": settings.degraded_modes(),
        "providers": {
            "llm": settings.llm_provider_effective,
            "search": settings.search_provider_effective,
            "map": settings.map_provider_effective,
            "weather": settings.weather_provider,
        },
        "config": {
            "scoring_version": get_scoring_config().version,
            "limits_version": get_limits_config().version,
            "ttl_version": get_ttl_config().version,
            "pricing_version": pricing.version,
            # 关键：单价未校准时必须显式暴露，避免成本报表被误读为真实支出
            "pricing_calibrated": pricing.is_fully_calibrated,
        },
        # ★ 这里必须报**真正在熔断的那两个值**。
        # 熔断器读的是 `config/limits.yaml`（`plan_service` 里
        # `breaker=limits.cost.circuit_breaker`），而 `Settings` 里的同名变量只是个
        # 镜像 —— 曾经报的是后者，于是把环境变量改成 99 会让 /health 兴高采烈地显示
        # “99 元熔断”，而实际仍然在 1 元处被拦下。
        # 两个默认值恰好相等（1.0 / 0.30）才一直没人发现。
        "cost_breakers": {
            "plan_total_cny": get_limits_config().cost.circuit_breaker.plan_total_cny,
            "plan_search_cny": get_limits_config().cost.circuit_breaker.plan_search_cny,
        },
        "cost": cost_info,
    }


@router.get("/health")
async def health(db: AsyncSession = Depends(get_db)) -> dict[str, object]:
    from app.schemas.common import ApiMeta, ok_envelope

    db_info = await _db_probe(db)
    cost_info = await _cost_probe(db)
    return ok_envelope(_payload(db_info, cost_info), ApiMeta())


@router.get("/health/ready", response_model=None)
async def ready(db: AsyncSession = Depends(get_db)) -> dict[str, object]:
    from fastapi.responses import JSONResponse

    from app.schemas.common import ApiMeta, ok_envelope

    db_info = await _db_probe(db)
    # 就绪探针只关心"能不能接流量"，日成本不参与判定，但报出来不会有坏处：
    # 它同样是"现在到底能干什么"的一部分，而且库已经查过一次了。
    payload = _payload(db_info, await _cost_probe(db))
    body = ok_envelope(payload, ApiMeta())
    if payload["status"] != "ok":
        return JSONResponse(status_code=503, content=body)  # type: ignore[return-value]
    return body
