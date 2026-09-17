"""客户端上报：纠错反馈（FR-11.5）与错误日志（FR-13.5）。

这两个端点是**唯二**由浏览器直接写入数据库的入口，所以它们共享同一套约束：

1. **限流按 IP**（``limits.yaml`` 的 ``feedback_per_ip_per_hour``，两块共用一个令牌桶口径，    但键分开 —— 错误上报量大得多，混在一起会把"用户真的想纠错"挤掉）。
2. **长度上限来自配置**，超长直接 422 说清楚，而不是悄悄截断后存进去
   （截断会让用户以为自己的描述被收到了）。
3. **不存在的 trip/place 一律 404，而不是让它撞外键**：撞外键抛的是
   ``IntegrityError``，会被包成 500「数据库暂时不可用」——
   数据库完全健康，错的是这条上报的引用。
4. **写进库的上下文必须脱敏**：客户端送来的字符串里可能混着被拼进报错信息的 Key。
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_limits_config
from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger, get_request_id, scrub
from app.db.models import ErrorLog, Feedback, Place, Trip
from app.db.session import get_db
from app.schemas.common import ApiMeta, ok_envelope
from app.schemas.reports import ErrorReportIn, ErrorReportOut, FeedbackIn, FeedbackOut
from app.services.cache import sha256_hex
from app.services.rate_limit import RateLimiter

__all__ = ["router"]

log = get_logger("api.reports")

router = APIRouter(tags=["reports"])

#: 上下文里最多保留多少项、每个值多长。够描述现场，又不至于把这张表变成数据仓库。
_MAX_CONTEXT_KEYS = 20
_MAX_CONTEXT_VALUE_CHARS = 200


def _sanitize_context(raw: dict[str, Any] | None) -> dict[str, str] | None:
    """把客户端上下文压成「键 → 脱敏后的短字符串」。

    刻意不保留嵌套结构：客户端上报的 ``context`` 是不可信输入，
    而这张表的用途是给人看错误现场。所有值都过 ``scrub`` ——
    PRD §24 的脱敏要求对"我们自己的表"同样成立。
    """
    if not raw:
        return None
    out: dict[str, str] = {}
    for key, value in list(raw.items())[:_MAX_CONTEXT_KEYS]:
        if not isinstance(key, str) or not key:
            continue
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
        out[key[:40]] = scrub(text, max_len=_MAX_CONTEXT_VALUE_CHARS)
    return out or None


def _require_text(value: str | None, *, field_name: str, max_chars: int) -> str | None:
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    if len(text) > max_chars:
        raise AppError(
            ErrorCode.INVALID_INPUT,
            f"{field_name}过长（{len(text)} 字），最多 {max_chars} 字",
            hint="把最关键的一句写前面即可。",
            context={"max_chars": max_chars},
        )
    return text


async def _enforce_rate_limit(
    db: AsyncSession, request: Request, *, key_prefix: str, limit: int
) -> None:
    """按 IP 限流。键用哈希：这条记录本身也不该存明文 IP（PRD AC-12.5）。"""
    client = request.client
    ip_hash = sha256_hex(client.host) if client and client.host else "unknown"
    result = await RateLimiter(db).hit(f"{key_prefix}:{ip_hash}", limit=limit, window_s=3600)
    if not result.allowed:
        raise AppError(
            ErrorCode.RATE_LIMITED,
            "上报太频繁了，请稍后再试",
            hint=f"这一小时内最多 {limit} 次，{result.retry_after_s} 秒后恢复。",
            context={"retry_after_s": result.retry_after_s},
        )


async def _optional_uuid(value: str | None, *, field_name: str) -> uuid.UUID | None:
    if value is None:
        return None
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise AppError(
            ErrorCode.INVALID_INPUT,
            f"{field_name} 不是合法的 id",
            hint="正常路径下这个值由页面自动带上，如果它坏了请刷新页面重试。",
            context={field_name: value[:64]},
        ) from exc


@router.post("/feedback")
async def create_feedback(
    payload: FeedbackIn, request: Request, db: AsyncSession = Depends(get_db)
) -> JSONResponse:
    """纠错反馈（PRD §7.2）。写入 ``feedback`` 表，MVP 不做工单流转。"""
    rl = get_limits_config().rate_limit
    await _enforce_rate_limit(
        db, request, key_prefix="feedback", limit=rl.feedback_per_ip_per_hour
    )
    message = _require_text(
        payload.message, field_name="反馈内容", max_chars=rl.feedback_message_max_chars
    )

    place_id = await _optional_uuid(payload.place_id, field_name="place_id")
    if place_id is not None:
        exists = (
            await db.execute(select(Place.id).where(Place.id == place_id))
        ).scalar_one_or_none()
        if exists is None:
            raise AppError(ErrorCode.PLACE_NOT_FOUND, context={"place_id": str(place_id)})

    trip_id = await _optional_uuid(payload.trip_id, field_name="trip_id")
    if trip_id is not None:
        exists = (await db.execute(select(Trip.id).where(Trip.id == trip_id))).scalar_one_or_none()
        if exists is None:
            raise AppError(ErrorCode.TRIP_NOT_FOUND, context={"trip_id": str(trip_id)})

    row = Feedback(
        category=payload.category,
        message=message,
        contact=payload.contact,
        place_id=place_id,
        trip_id=trip_id,
    )
    db.add(row)
    # ★ flush 之后必须 refresh ★ ``feedback.status`` 是**服务端默认值**（``open``），
    # 不 refresh 就去读它会触发一次隐式 lazy load —— 在 async 会话里那是 MissingGreenlet 异常，
    # 表现为一个与"写反馈"毫无关系的 500。
    await db.flush()
    await db.refresh(row)
    await db.commit()
    # 日志只记"有人报了哪一类"，不记内容与联系方式 —— 用户把这段话交给我们是为了
    # 修数据，不是为了让它出现在日志里（PRD AC-12.5）。
    log.info(
        "收到纠错反馈",
        extra={
            "event": "feedback.received",
            "code": payload.category,
            "context": {
                "has_message": message is not None,
                "has_place": place_id is not None,
                "has_trip": trip_id is not None,
            },
        },
    )
    out = FeedbackOut(
        id=str(row.id),
        status=row.status,
        note="已记录。数据问题会在人工复核时核对来源后修正，不会自动覆盖知识库。",
    )
    return JSONResponse(
        status_code=200,
        content=ok_envelope(out.model_dump(), ApiMeta(request_id=get_request_id())),
    )


@router.post("/errors")
async def report_error(
    payload: ErrorReportIn, request: Request, db: AsyncSession = Depends(get_db)
) -> JSONResponse:
    """浏览器错误上报（PRD FR-13.5："前端全局 error boundary + 本地 ``error_logs`` 表"）。

    刻意**不**返回任何内部信息：这个端点对公网开放，而它唯一的用途是收下一条记录。
    """
    rl = get_limits_config().rate_limit
    await _enforce_rate_limit(db, request, key_prefix="errors", limit=rl.feedback_per_ip_per_hour)

    # 与纠错反馈相反：这里"只有空白"的留言必须被拒 —— 一条没有内容的错误日志
    # 在表里占一行，却什么也说明不了（``min_length=1`` 拦不住纯空格）。
    raw_message = _require_text(
        payload.message, field_name="错误信息", max_chars=rl.error_report_max_chars
    )
    if raw_message is None:
        raise AppError(
            ErrorCode.INVALID_INPUT,
            "错误信息不能只有空白",
            hint="请带上浏览器报错的那句话。",
        )
    message = scrub(raw_message, max_len=rl.error_report_max_chars)
    request_id = await _optional_uuid(payload.request_id, field_name="request_id")

    row = ErrorLog(
        request_id=request_id,
        level=payload.level,
        component=payload.component,
        code=payload.code,
        message=message,
        context=_sanitize_context(payload.context),
    )
    db.add(row)
    await db.flush()
    await db.refresh(row)
    await db.commit()

    log.warning(
        "浏览器上报错误",
        extra={
            "event": "client.error",
            "code": payload.code or "",
            "context": {"component": payload.component, "level": payload.level},
        },
    )
    out = ErrorReportOut(id=int(row.id))
    return JSONResponse(
        status_code=200,
        content=ok_envelope(out.model_dump(), ApiMeta(request_id=get_request_id())),
    )
