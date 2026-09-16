"""规划与行程 API（PRD §7.2）。

端点：
    POST /trips:plan              受理规划（202）或同步返回（?sync=true）
    GET  /trips/{request_id}/stream  SSE 进度流
    GET  /trips/{trip_id}         完整行程
    POST /trips/{trip_id}/revise  自然语言修改 → 新版本
    POST /trips/{trip_id}/undo    撤销到上一版
    POST /trips/{trip_id}/share   生成/取消公开链接

三个刻意的设计：
1. **异步受理 + SSE**：生成一次规划要几秒，同步等会让移动端超时；202 + 事件流
   也让"等待体验"有真实进度（PRD §5.3），而不是一个转圈。
2. **后台任务自己开 session**：FastAPI 的依赖会话在响应结束后就关闭了，
   后台任务复用它会在提交时炸掉。所以 ``_run_plan`` 用 ``get_sessionmaker()`` 新建。
3. **归属校验**：trip 只能被它自己的 session 修改/撤销/分享（否则任何拿到
   trip_id 的人都能改别人的行程）。trip_id 是不可枚举的 uuid，再加一层归属判断。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Sequence
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_limits_config, get_scoring_config
from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger, get_request_id
from app.db.models import Trip, TripRequest
from app.db.session import get_db, get_sessionmaker
from app.domain.intent import parse_intent
from app.providers.registry import build_providers
from app.schemas.common import ApiMeta, ok_envelope
from app.schemas.trips import (
    PlanAcceptedOut,
    PlanRequest,
    RevisionOut,
    RevisionRequest,
    ShareOut,
    ShareRequest,
)
from app.services.cache import sha256_hex
from app.services.plan_events import PlanEvent, get_event_bus
from app.services.plan_service import (
    PlanOutcome,
    PlanProgress,
    PlanService,
    build_intent,
    intent_to_dict,
)
from app.services.session import get_session_id, set_session_cookie
from app.services.trip_service import (
    best_route_signature,
    build_diff,
    find_trip_by_request,
    load_trip,
    plan_request_from_trip,
    record_revision,
    serialize_trip,
    set_visibility,
    undo_last_revision,
)

__all__ = ["router"]

log = get_logger("api.trips")

router = APIRouter(tags=["trips"])

#: SSE 心跳间隔。没有它，中间的代理会在静默 60 秒后掐断连接。
_SSE_KEEPALIVE_S = 15


# ── 公共小工具 ──────────────────────────────────────────────────────────────


def _json(session_id: uuid.UUID, content: dict[str, Any], *, status_code: int = 200) -> JSONResponse:
    """统一的 JSON 响应 + 会话 cookie（见 ``set_session_cookie`` 的说明）。"""
    response = JSONResponse(status_code=status_code, content=content)
    set_session_cookie(response, session_id)
    return response


def _meta(**kwargs: Any) -> ApiMeta:
    return ApiMeta(request_id=get_request_id(), **kwargs)


def _llm_meta(outcome: PlanOutcome) -> dict[str, Any] | None:
    """把 LLM 使用情况放进响应元信息（与日志、cost_logs 同一份事实）。"""
    return outcome.llm.as_dict() if outcome.llm is not None else None


def _client_hashes(request: Request | None) -> tuple[str | None, str | None]:
    """客户端标识只存**哈希**，不存原始 IP（PRD AC-12.5）。"""
    if request is None:
        return None, None
    client = request.client
    ip_hash = sha256_hex(client.host) if client and client.host else None
    ua = request.headers.get("user-agent")
    ua_hash = sha256_hex(ua) if ua else None
    return ip_hash, ua_hash


def _rate_limit_keys(session_id: uuid.UUID, ip_hash: str | None) -> list[tuple[str, int, int]]:
    rl = get_limits_config().rate_limit
    keys: list[tuple[str, int, int]] = [
        (f"plan:req:ip:{ip_hash or 'unknown'}", rl.requests_per_ip_per_minute, 60),
        (f"plan:cold:session:{session_id}", rl.cold_plans_per_session_per_day, 86_400),
    ]
    if ip_hash:
        keys.append((f"plan:cold:ip:{ip_hash}", rl.cold_plans_per_ip_per_day, 86_400))
    return keys


def _requested_days(trip: Trip) -> int:
    """用户在需求里填的天数。

    与 ``trip.days`` 那种"实际排出来几天"必须区分开：Diff 说的是"你的需求改了什么"，
    而"库里的地点只够排 2 天"是另一件事（由 ``days_short`` 降级标记单独说明）。
    用实际天数去比，"改成 3 天"会显示成什么都没改（两边都是 1）。
    """
    return int((trip.intent_snapshot or {}).get("days") or trip.days or 1)


def _as_uuid(value: str) -> uuid.UUID:
    """路径里的 id 先当字符串收，再自己转 uuid。

    ★ 为什么不把参数类型直接写成 ``uuid.UUID`` ★
    那样 FastAPI 会对 ``/trips/abc123`` 返回 422 INVALID_INPUT（"你提交的内容不合法"），
    但用户的感受是"这个行程不存在"。而且错误码语义测试早就钉住了：
    行程路径上的 404 必须是 TRIP_NOT_FOUND。一个拼错的链接不该被描述成输入错误。
    """
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise AppError(
            ErrorCode.TRIP_NOT_FOUND,
            "这个行程不存在或链接有误",
            hint="请从「我之前的行程」里重新打开。",
            context={"id": value[:64]},
        ) from exc


def _ensure_owner(trip: Trip, session_id: uuid.UUID) -> None:
    if trip.session_id != session_id:
        raise AppError(
            ErrorCode.FORBIDDEN,
            "这个行程不属于当前会话",
            hint="请回到创建它的浏览器（游客行程按浏览器会话隔离）。",
            context={"trip_id": str(trip.id)},
        )


# ── 规划 ────────────────────────────────────────────────────────────────────


@router.post("/trips:plan")
async def create_plan(
    payload: PlanRequest,
    background: BackgroundTasks,
    request: Request,
    sync: bool = Query(default=False, description="同步返回完整行程（调试与端到端测试用）"),
    db: AsyncSession = Depends(get_db),
    session_id: uuid.UUID = Depends(get_session_id),
) -> JSONResponse:
    ip_hash, ua_hash = _client_hashes(request)
    request_id = uuid.uuid4()
    keys = _rate_limit_keys(session_id, ip_hash)

    if sync:
        outcome = await _run_inline(db, session_id, payload, request_id, ip_hash, ua_hash, keys)
        trip = await load_trip(db, outcome.trip_id)
        out = await serialize_trip(db, trip)
        return _json(
            session_id,
            ok_envelope(
                out.model_dump(mode="json"),
                _meta(
                    cached=outcome.cached,
                    degraded_modes=list(outcome.degraded_modes),
                    elapsed_ms=outcome.elapsed_ms,
                    llm=_llm_meta(outcome),
                ),
            ),
        )

    # 预先建好通道：否则"后台任务先跑完、客户端后订阅"这一段会丢事件
    get_event_bus().channel(str(request_id))
    background.add_task(
        _run_plan,
        request_id=request_id,
        session_id=session_id,
        payload=payload,
        ip_hash=ip_hash,
        ua_hash=ua_hash,
        rate_limit_keys=keys,
    )
    accepted = PlanAcceptedOut(
        request_id=str(request_id),
        stream_url=f"/api/v1/trips/{request_id}/stream",
    )
    return _json(session_id, ok_envelope(accepted.model_dump(), _meta()), status_code=202)


async def _run_inline(
    db: AsyncSession,
    session_id: uuid.UUID,
    payload: PlanRequest,
    request_id: uuid.UUID,
    ip_hash: str | None,
    ua_hash: str | None,
    rate_limit_keys: Sequence[tuple[str, int, int]],
) -> PlanOutcome:
    service = PlanService(db=db, session_id=session_id, providers=build_providers())
    outcome = await service.plan(
        payload,
        request_id=request_id,
        ip_hash=ip_hash,
        user_agent_hash=ua_hash,
        rate_limit_keys=rate_limit_keys,
    )
    await db.commit()
    return outcome


async def _run_plan(
    *,
    request_id: uuid.UUID,
    session_id: uuid.UUID,
    payload: PlanRequest,
    ip_hash: str | None,
    ua_hash: str | None,
    rate_limit_keys: Sequence[tuple[str, int, int]],
) -> None:
    """后台跑一次规划并把进度发给事件总线。

    必须自己开 session：请求级会话在响应结束时已经关闭。
    """
    bus = get_event_bus()
    key = str(request_id)

    async def sink(progress: PlanProgress) -> None:
        # stage 0 是"刚开始"，其余才是进度。PRD §7.3 的事件序列里它们是两个事件名，
        # 前端据此决定"显示生成中"还是"更新进度条"。
        event = "plan.started" if progress.stage == 0 else "plan.progress"
        payload: dict[str, Any] = {
            "stage": progress.stage,
            "key": progress.key,
            "label": progress.label,
            "pct": progress.pct,
            "detail": dict(progress.detail),
        }
        if progress.stage == 0:
            # PRD §7.3：plan.started 带 request_id，客户端才能把"这条流"与"那次请求"对上
            payload["request_id"] = key
        bus.publish(key, event, payload)

    async with get_sessionmaker()() as db:
        service = PlanService(db=db, session_id=session_id, providers=build_providers())
        try:
            outcome = await service.plan(
                payload,
                on_progress=sink,
                request_id=request_id,
                ip_hash=ip_hash,
                user_agent_hash=ua_hash,
                rate_limit_keys=rate_limit_keys,
            )
            await db.commit()
        except AppError as exc:
            await db.rollback()
            bus.publish(
                key,
                "plan.failed",
                {"code": str(exc.code), "message": exc.message, "hint": exc.hint},
            )
        except Exception:
            await db.rollback()
            log.error("后台规划失败", exc_info=True, extra={"event": "plan.background_error"})
            bus.publish(
                key,
                "plan.failed",
                {
                    "code": str(ErrorCode.INTERNAL),
                    "message": "服务出了点问题，请重试。",
                    "hint": "若持续出现，请把响应头里的 X-Request-Id 反馈给我们。",
                },
            )
        else:
            bus.publish(
                key,
                "plan.completed",
                {
                    "trip_id": str(outcome.trip_id),
                    "route_count": outcome.route_count,
                    "cached": outcome.cached,
                    "degraded_modes": list(outcome.degraded_modes),
                    "llm": _llm_meta(outcome),
                },
            )
        finally:
            bus.close(key)


# ── SSE ─────────────────────────────────────────────────────────────────────


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.get("/trips/{request_id}/stream")
async def stream_plan(
    request_id: str,
    db: AsyncSession = Depends(get_db),
    session_id: uuid.UUID = Depends(get_session_id),
) -> StreamingResponse:
    rid = _as_uuid(request_id)  # 非法 id → TRIP_NOT_FOUND（而不是 422）
    bus = get_event_bus()
    channel = bus.existing(str(rid))

    if channel is None:
        # 通道已经被回收（或进程重启过）。此时唯一的诚实做法是**回查数据库**：
        # 行程存在就告诉客户端"已经完成"，不存在就说它不存在 —— 而不是挂着不响应。
        trip = await find_trip_by_request(db, rid)
        if trip is None:
            raise AppError(ErrorCode.TRIP_NOT_FOUND, context={"request_id": str(rid)})
        completed = _sse(
            "plan.completed",
            {
                "trip_id": str(trip.id),
                "route_count": trip.route_count,
                "cached": True,
                "degraded_modes": list(trip.degraded_modes or []),
            },
        )
        return _stream_response([completed], session_id)

    queue, backlog = channel.subscribe()

    async def events() -> AsyncIterator[str]:
        try:
            for event, data in backlog:
                yield _sse(event, data)
                if event in ("plan.completed", "plan.failed"):
                    return
            if channel.closed:
                return
            while True:
                try:
                    item: PlanEvent | None = await asyncio.wait_for(queue.get(), timeout=_SSE_KEEPALIVE_S)
                except TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                if item is None:
                    return
                event, data = item
                yield _sse(event, data)
                if event in ("plan.completed", "plan.failed"):
                    return
        finally:
            channel.unsubscribe(queue)

    return _stream_response(events(), session_id)


def _stream_response(body: Any, session_id: uuid.UUID) -> StreamingResponse:
    response = StreamingResponse(
        body,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # Nginx 默认会缓冲整个响应，那样 SSE 就变成"最后一次性到达"
            "X-Accel-Buffering": "no",
        },
    )
    set_session_cookie(response, session_id)
    return response


# ── 读取 / 修改 / 撤销 / 分享 ───────────────────────────────────────────────


async def _owned_trip(db: AsyncSession, trip_id: uuid.UUID, session_id: uuid.UUID) -> Trip:
    trip = await load_trip(db, trip_id)
    _ensure_owner(trip, session_id)
    return trip


@router.get("/trips/{trip_id}")
async def get_trip(
    trip_id: str,
    db: AsyncSession = Depends(get_db),
    session_id: uuid.UUID = Depends(get_session_id),
) -> JSONResponse:
    trip = await _owned_trip(db, _as_uuid(trip_id), session_id)
    out = await serialize_trip(db, trip)
    return _json(session_id, ok_envelope(out.model_dump(mode="json"), _meta()))


@router.post("/trips/{trip_id}/revise")
async def revise_trip(
    trip_id: str,
    body: RevisionRequest,
    db: AsyncSession = Depends(get_db),
    session_id: uuid.UUID = Depends(get_session_id),
) -> JSONResponse:
    """自然语言修改（PRD FR-08）。

    ★ 修改**不触发联网搜索**（AC-8.2）★
    指令由规则引擎解析成约束，然后走同一套本地重算路径，成本仍为 0。
    解析不出任何增量时**不报错**，而是回问澄清（AC-8.7）。

    ★ ``delta.intent`` 必须真的参与重算 ★
    规则引擎有两类输出：**约束**（排除地点、步行上限…）与**意图字段**
    （天数/人数/节奏/预算/偏好）。早期实现只把约束传下去、拿原 payload 重算，
    于是"改成 3 天""预算改成 900 元"这类指令既不报错、也不改任何东西，
    却返回了一个新版本和一个 Diff 为空的"修改成功"——用户的要求被静默丢弃。
    现在把解析后的意图整体写回规划入参（``free_text`` 留空，避免原始文本
    再次覆盖用户这次的修改）。
    """
    trip = await _owned_trip(db, _as_uuid(trip_id), session_id)
    raw_request = (
        await db.execute(select(TripRequest).where(TripRequest.id == trip.request_id))
    ).scalar_one_or_none()
    payload = plan_request_from_trip(
        dict(raw_request.raw_input) if raw_request else {}, trip.intent_snapshot or {}
    )

    _, _, base_parse = build_intent(payload, limits=get_limits_config(), scoring=get_scoring_config())
    delta = parse_intent(
        body.instruction,
        base_parse.intent,
        limits=get_limits_config(),
        scoring=get_scoring_config(),
    )
    new_constraints = tuple(delta.constraints) if delta.constraints else ()
    if not new_constraints and not delta.applied_rules:
        # 不报错：告诉用户可以怎么改，而不是丢一个"看不懂"的失败
        return _json(
            session_id,
            ok_envelope(
                RevisionOut(
                    trip_id=str(trip.id),
                    revision_no=trip.revision_no,
                    diff={},
                    needs_clarification=(
                        f"没能理解「{body.instruction}」。你可以在路线上直接排除某个地点，"
                        "或说明想增加/减少哪类地点。"
                    ),
                ).model_dump(mode="json"),
                _meta(),
            ),
        )

    before = await best_route_signature(db, trip.id)
    # 修改不消耗"冷规划"配额，因此不取客户端标识（限流只在 ``create_plan`` 里做）
    ip_hash, ua_hash = _client_hashes(None)
    service = PlanService(db=db, session_id=session_id, providers=build_providers())
    revised_payload = plan_request_from_trip({}, intent_to_dict(delta.intent))
    outcome = await service.plan(
        revised_payload,
        extra_constraints=new_constraints,
        parent_trip=trip,
        allow_cache=False,
        ip_hash=ip_hash,
        user_agent_hash=ua_hash,
    )
    await db.flush()
    result = await load_trip(db, outcome.trip_id)
    await db.flush()
    after = await best_route_signature(db, result.id)
    # Diff 比的是**请求里的天数**，不是实际排出来的天数：用户改的是需求，
    # "库里的地点只够排 2 天"是另一件事（由 ``days_short`` 降级标记单独说明）。
    # 拿实际天数去比会让"改成 3 天"看起来什么都没改（两者都是 1）。
    diff = build_diff(
        before,
        after,
        days_before=_requested_days(trip),
        days_after=_requested_days(result),
    )
    await record_revision(
        db,
        trip=trip,
        instruction=body.instruction,
        constraints=new_constraints,
        diff=diff,
        result_trip=result,
    )
    await db.commit()

    out = RevisionOut(
        trip_id=str(result.id),
        revision_no=result.revision_no,
        diff=diff.as_dict(),
    )
    return _json(session_id, ok_envelope(out.model_dump(mode="json"), _meta()))


@router.post("/trips/{trip_id}/undo")
async def undo_trip(
    trip_id: str,
    db: AsyncSession = Depends(get_db),
    session_id: uuid.UUID = Depends(get_session_id),
) -> JSONResponse:
    trip = await _owned_trip(db, _as_uuid(trip_id), session_id)
    previous = await undo_last_revision(db, trip)
    _ensure_owner(previous, session_id)
    out = await serialize_trip(db, previous)
    return _json(session_id, ok_envelope(out.model_dump(mode="json"), _meta()))


@router.post("/trips/{trip_id}/share")
async def share_trip(
    trip_id: str,
    body: ShareRequest,
    db: AsyncSession = Depends(get_db),
    session_id: uuid.UUID = Depends(get_session_id),
) -> JSONResponse:
    trip = await _owned_trip(db, _as_uuid(trip_id), session_id)
    share: ShareOut = await set_visibility(db, trip, public=body.public)
    await db.commit()
    return _json(session_id, ok_envelope(share.model_dump(), _meta()))
