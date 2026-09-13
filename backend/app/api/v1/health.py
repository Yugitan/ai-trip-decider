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


def _payload(db_info: dict[str, object]) -> dict[str, object]:
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
        "cost_breakers": {
            "plan_total_cny": settings.plan_cost_circuit_breaker_cny,
            "plan_search_cny": settings.search_cost_circuit_breaker_cny,
        },
    }


@router.get("/health")
async def health(db: AsyncSession = Depends(get_db)) -> dict[str, object]:
    from app.schemas.common import ApiMeta, ok_envelope

    db_info = await _db_probe(db)
    return ok_envelope(_payload(db_info), ApiMeta())


@router.get("/health/ready", response_model=None)
async def ready(db: AsyncSession = Depends(get_db)) -> dict[str, object]:
    from fastapi.responses import JSONResponse

    from app.schemas.common import ApiMeta, ok_envelope

    db_info = await _db_probe(db)
    payload = _payload(db_info)
    body = ok_envelope(payload, ApiMeta())
    if payload["status"] != "ok":
        return JSONResponse(status_code=503, content=body)  # type: ignore[return-value]
    return body
