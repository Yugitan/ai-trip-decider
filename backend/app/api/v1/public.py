"""公开端点：免登录查看分享的行程（PRD FR-09）。

只读 + 复制，没有任何"修改别人的行程"的能力。取消分享（``public=false``）后
链接**立刻 404**（AC-9.7）—— 这是分享功能的真实边界，不是"隐藏在前端"。
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger, get_request_id
from app.db.session import get_db
from app.schemas.common import ApiMeta, ok_envelope
from app.services.session import get_session_id, set_session_cookie
from app.services.trip_service import (
    copy_shared_trip,
    find_trip_by_share_slug,
    serialize_trip,
)

__all__ = ["router"]

log = get_logger("api.public")

router = APIRouter(tags=["public"])


def _json(session_id: uuid.UUID, content: dict[str, Any], *, status_code: int = 200) -> JSONResponse:
    response = JSONResponse(status_code=status_code, content=content)
    set_session_cookie(response, session_id)
    return response


@router.get("/public/trips/{slug}")
async def get_shared_trip(
    slug: str,
    db: AsyncSession = Depends(get_db),
    session_id: uuid.UUID = Depends(get_session_id),
) -> JSONResponse:
    """公开分享页的数据源。**无鉴权**，但只返回已公开的行程。"""
    trip = await find_trip_by_share_slug(db, slug)
    out = await serialize_trip(db, trip)
    log.info(
        "读取分享行程",
        extra={"event": "share.view", "context": {"slug": slug, "trip_id": str(trip.id)}},
    )
    return _json(
        session_id,
        ok_envelope(out.model_dump(mode="json"), ApiMeta(request_id=get_request_id())),
    )


@router.post("/public/trips/{slug}/copy")
async def copy_shared_trip_endpoint(
    slug: str,
    db: AsyncSession = Depends(get_db),
    session_id: uuid.UUID = Depends(get_session_id),
) -> JSONResponse:
    """「复制这套路线」：生成一份属于当前访客的可编辑副本（AC-9.4）。"""
    source = await find_trip_by_share_slug(db, slug)
    copied = await copy_shared_trip(db, source, session_id)
    await db.commit()
    out = await serialize_trip(db, copied)
    return _json(
        session_id,
        ok_envelope(out.model_dump(mode="json"), ApiMeta(request_id=get_request_id())),
        status_code=201,
    )
