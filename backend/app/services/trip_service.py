"""行程的读取、序列化、分享、修改与撤销（PRD FR-08 / FR-09 / FR-10）。

分工：
    ``plan_service``  负责"从需求生成方案"（写）；
    ``trip_service``  负责"把已存在的行程读出来、分享出去、改一改"（读 + 派生）。

两条不容易做对、因此写在这里的规则：

1. **修改（revise）不触发联网搜索**（AC-8.2）：修改走的是同一套**本地**规划路径，
   自由文本由规则引擎解析成约束后重新组合，只读知识库。这与"成本核心"直接相关 ——
   用户点十次"别去广州塔"不该产生十次搜索费用。
2. **撤销（undo）是沿 revision 链回退，不是原地改**：每次修改都写一条新 trip
   （``parent_trip_id`` 指向上一版），历史版本因此天然可回看、可回滚，
   Diff 也不需要额外快照 —— 两版 trip 本身就是快照。
"""

from __future__ import annotations

import secrets
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger
from app.db.models import City, Trip, TripRequest, TripRevision, TripRoute, TripRouteStop
from app.domain.models import Constraint
from app.schemas.trips import (
    PlanRequest,
    RevisionOut,
    ShareOut,
    TripOut,
    TripRouteOut,
    TripStopOut,
)

log = get_logger("trip")

__all__ = [
    "build_diff",
    "copy_shared_trip",
    "find_trip_by_request",
    "find_trip_by_share_slug",
    "load_trip",
    "new_share_slug",
    "serialize_trip",
    "set_visibility",
    "undo_last_revision",
]

#: 撤销的步数**不设上限**：每一版都是一个完整 trip，回看与回滚都只是沿着
#: `parent_trip_id` 走。PRD AC-8.5 要求"保留最近 5 次"，我们做得更多（全部保留）——
#: 清理旧版本是运维策略，不是功能边界，宁可多留也不要让用户"撤销到一半就没有了"。
#: 复制来源的 ``Trip.source``：这种 trip 的 ``parent_trip_id`` 是**来源（fork）**，
#: 不是上一版，撤销时不能沿它往回走（见 ``undo_last_revision``）。
_COPY_SOURCES = frozenset({"plan_cache", "copied"})

_BASE62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_SLUG_LENGTH = 12


def new_share_slug() -> str:
    """生成不可枚举的分享 slug（PRD AC-9.1：≥10 位 base62）。"""
    return "".join(secrets.choice(_BASE62) for _ in range(_SLUG_LENGTH))


async def load_trip(db: AsyncSession, trip_id: uuid.UUID) -> Trip:
    trip = (await db.execute(select(Trip).where(Trip.id == trip_id))).scalar_one_or_none()
    if trip is None:
        raise AppError(ErrorCode.TRIP_NOT_FOUND, context={"trip_id": str(trip_id)})
    return trip


async def find_trip_by_request(db: AsyncSession, request_id: uuid.UUID) -> Trip | None:
    """按 ``request_id`` 取 trip。幂等返回时前端手里只有 request_id，需要它来定位。"""
    return (
        await db.execute(select(Trip).where(Trip.request_id == request_id).limit(1))
    ).scalar_one_or_none()


async def find_trip_by_share_slug(db: AsyncSession, slug: str) -> Trip:
    trip = (
        await db.execute(
            select(Trip).where(Trip.share_slug == slug, Trip.is_public.is_(True)).limit(1)
        )
    ).scalar_one_or_none()
    if trip is None:
        # 私有/取消分享的链接必须**立刻 404**（AC-9.7），因此这里查的就是 is_public
        raise AppError(ErrorCode.SHARE_NOT_FOUND, context={"slug": slug})
    return trip


async def serialize_trip(db: AsyncSession, trip: Trip) -> TripOut:
    """把 trip 及其路线/站点组装成 API 输出。"""
    city = (
        await db.execute(select(City).where(City.id == trip.city_id))
    ).scalar_one_or_none()
    routes = (
        await db.execute(
            select(TripRoute).where(TripRoute.trip_id == trip.id).order_by(TripRoute.sort_order)
        )
    ).scalars().all()
    stops_by_route: dict[uuid.UUID, list[TripRouteStop]] = {}
    if routes:
        rows = (
            await db.execute(
                select(TripRouteStop)
                .where(TripRouteStop.trip_route_id.in_([route.id for route in routes]))
                .order_by(TripRouteStop.trip_route_id, TripRouteStop.seq)
            )
        ).scalars().all()
        for stop in rows:
            stops_by_route.setdefault(stop.trip_route_id, []).append(stop)

    intent = trip.intent_snapshot or {}
    return TripOut(
        trip_id=str(trip.id),
        request_id=str(trip.request_id),
        city=city.slug if city else "",
        title=trip.title,
        days=trip.days,
        revision_no=trip.revision_no,
        route_count=trip.route_count,
        route_count_note=intent.get("route_count_note"),
        intent=intent,
        degraded_modes=list(trip.degraded_modes or []),
        total_cost_cny=Decimal(str(trip.total_cost_cny or 0)),
        generation_ms=trip.generation_ms,
        created_at=trip.created_at.isoformat() if trip.created_at else None,
        is_public=trip.is_public,
        share_slug=trip.share_slug,
        routes=[_route_out(route, stops_by_route.get(route.id, [])) for route in routes],
    )


def _route_out(route: TripRoute, stops: Sequence[TripRouteStop]) -> TripRouteOut:
    report = route.feasibility_report or {}
    breakdown = route.score_breakdown or {}
    return TripRouteOut(
        id=str(route.id),
        label=route.label,
        archetype=route.archetype,
        theme=route.theme,
        name=route.name,
        one_liner=route.one_liner,
        place_count=route.place_count,
        total_duration_min=route.total_duration_min,
        walking_distance_m=route.walking_distance_m,
        transit_time_min=route.transit_time_min,
        transit_distance_m=route.transit_distance_m,
        budget_min=route.budget_min,
        budget_max=route.budget_max,
        budget_scope=route.budget_scope,
        # 「金额含估算值」以**落库的报告**为准，不再写死 True：
        # 写死会让同一份响应里 ``budget_estimated`` 与
        # ``feasibility.budget_estimated`` 互相矛盾（有项目全部有明确价格时后者为 False）。
        budget_estimated=bool(report.get("budget_estimated", True)),
        budget_unknown_items=list(report.get("budget_unknown_items") or []),
        recommend_score=route.recommend_score,
        score_breakdown=dict(breakdown),
        feasibility=dict(report),
        best_for=list(route.best_for or []),
        highlights=list(route.highlights or []),
        pros=list(route.pros or []),
        cons=list(route.cons or []),
        recommendation_reason=route.recommendation_reason,
        route_source=route.route_source,
        template_route_id=str(route.template_route_id) if route.template_route_id else None,
        stops=[_stop_out(stop) for stop in stops],
    )


def _stop_out(stop: TripRouteStop) -> TripStopOut:
    snapshot: dict[str, Any] = dict(stop.place_snapshot or {})
    return TripStopOut(
        seq=stop.seq,
        place_id=str(stop.place_id),
        name=str(snapshot.get("name", "")),
        category=str(snapshot.get("category", "")),
        latitude=float(snapshot.get("latitude", 0.0)),
        longitude=float(snapshot.get("longitude", 0.0)),
        district=snapshot.get("district"),
        arrive_time=stop.arrive_time.strftime("%H:%M"),
        depart_time=stop.depart_time.strftime("%H:%M"),
        stay_min=stop.stay_min,
        transport_mode=stop.transport_mode,
        transport_min=stop.transport_min,
        transport_distance_m=stop.transport_distance_m,
        transport_source=stop.transport_source,
        why_recommended=stop.why_recommended,
        tips=stop.tips,
        warnings=list(stop.warnings or []),
        snapshot=snapshot,
    )


# ════════════════════════════════════════════════════════════════════════════
# 分享
# ════════════════════════════════════════════════════════════════════════════


async def set_visibility(db: AsyncSession, trip: Trip, *, public: bool) -> ShareOut:
    """生成/取消公开链接（PRD AC-9.7）。取消后 slug 保留但链接立刻 404。"""
    if public and not trip.share_slug:
        trip.share_slug = await _unique_slug(db)
    trip.is_public = public
    trip.shared_at = datetime.now(UTC) if public else trip.shared_at
    await db.flush()
    slug = trip.share_slug or ""
    return ShareOut(
        slug=slug,
        url=f"{get_settings().frontend_url.rstrip('/')}/t/{slug}",
        is_public=public,
    )


async def _unique_slug(db: AsyncSession, *, attempts: int = 5) -> str:
    """slug 必须不可枚举，同时也要真的唯一（唯一约束会抛错，所以先查）。"""
    for _ in range(attempts):
        slug = new_share_slug()
        exists = (
            await db.execute(select(Trip.id).where(Trip.share_slug == slug).limit(1))
        ).scalar_one_or_none()
        if exists is None:
            return slug
    raise AppError(
        ErrorCode.INTERNAL,
        "生成分享链接失败，请重试",
        hint="如果持续出现，请把当前时间反馈给我们以便排查。",
    )


async def copy_shared_trip(db: AsyncSession, source: Trip, session_id: uuid.UUID) -> Trip:
    """「复制这套路线」：给当前访客生成一份**可编辑的副本**（AC-9.4）。

    刻意复制而不是引用：原作者的后续修改不应改变别人手里的行程。
    """
    request = TripRequest(
        session_id=session_id,
        raw_input={"copied_from": str(source.id)},
        intent=source.intent_snapshot,
        params_hash=f"copy:{uuid.uuid4().hex}",
        status="completed",
        completed_at=datetime.now(UTC),
    )
    db.add(request)
    await db.flush()

    trip = Trip(
        request_id=request.id,
        session_id=session_id,
        city_id=source.city_id,
        title=source.title,
        intent_snapshot=source.intent_snapshot,
        days=source.days,
        route_count=source.route_count,
        generation_ms=source.generation_ms,
        total_cost_cny=source.total_cost_cny,
        degraded_modes=list(source.degraded_modes or []),
        revision_no=source.revision_no,
        parent_trip_id=source.id,
        source="copied",
    )
    db.add(trip)
    await db.flush()

    routes = (
        await db.execute(select(TripRoute).where(TripRoute.trip_id == source.id))
    ).scalars().all()
    for route in routes:
        clone = TripRoute(
            trip_id=trip.id,
            label=route.label,
            archetype=route.archetype,
            theme=route.theme,
            name=route.name,
            one_liner=route.one_liner,
            total_duration_min=route.total_duration_min,
            total_distance_m=route.total_distance_m,
            walking_distance_m=route.walking_distance_m,
            transit_time_min=route.transit_time_min,
            transit_distance_m=route.transit_distance_m,
            budget_min=route.budget_min,
            budget_max=route.budget_max,
            budget_scope=route.budget_scope,
            place_count=route.place_count,
            recommend_score=route.recommend_score,
            score_breakdown=route.score_breakdown,
            best_for=list(route.best_for or []),
            highlights=list(route.highlights or []),
            pros=list(route.pros or []),
            cons=list(route.cons or []),
            recommendation_reason=route.recommendation_reason,
            feasibility_report=route.feasibility_report,
            polyline=route.polyline,
            route_source=route.route_source,
            template_route_id=route.template_route_id,
            sort_order=route.sort_order,
        )
        db.add(clone)
        await db.flush()
        stops = (
            await db.execute(
                select(TripRouteStop)
                .where(TripRouteStop.trip_route_id == route.id)
                .order_by(TripRouteStop.seq)
            )
        ).scalars().all()
        for stop in stops:
            db.add(
                TripRouteStop(
                    trip_route_id=clone.id,
                    seq=stop.seq,
                    place_id=stop.place_id,
                    place_snapshot=stop.place_snapshot,
                    arrive_time=stop.arrive_time,
                    depart_time=stop.depart_time,
                    stay_min=stop.stay_min,
                    transport_mode=stop.transport_mode,
                    transport_min=stop.transport_min,
                    transport_distance_m=stop.transport_distance_m,
                    transport_source=stop.transport_source,
                    why_recommended=stop.why_recommended,
                    tips=stop.tips,
                    source_refs=stop.source_refs,
                    warnings=list(stop.warnings or []),
                )
            )
    await db.flush()
    return trip


# ════════════════════════════════════════════════════════════════════════════
# 修改与撤销
# ════════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class DiffSummary:
    """两版行程的可读差异（AC-8.4）。"""

    removed: tuple[str, ...]
    added: tuple[str, ...]
    walking_before_m: int
    walking_after_m: int
    budget_before: Decimal | None
    budget_after: Decimal | None
    route_count_before: int
    route_count_after: int
    # 天数/人数是**行程**级属性，不是首要路线的签名，因此单独传进来。
    # 必须带上："改成 3 天"这类指令改变的就是它们，而它之前的 Diff
    # 只比较路线与预算，会输出一句"什么都没变"（与修改本身矛盾）。
    days_before: int | None = None
    days_after: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "removed": list(self.removed),
            "added": list(self.added),
            "walking_before_m": self.walking_before_m,
            "walking_after_m": self.walking_after_m,
            "walking_delta_m": self.walking_after_m - self.walking_before_m,
            "budget_before": None if self.budget_before is None else str(self.budget_before),
            "budget_after": None if self.budget_after is None else str(self.budget_after),
            "route_count_before": self.route_count_before,
            "route_count_after": self.route_count_after,
            "days_before": self.days_before,
            "days_after": self.days_after,
            "sentence": self.sentence(),
        }

    def sentence(self) -> str:
        parts: list[str] = []
        if self.days_before is not None and self.days_after is not None and self.days_before != self.days_after:
            parts.append(f"天数 {self.days_before} 天 → {self.days_after} 天")
        if self.removed:
            parts.append(f"移除了 {'、'.join(self.removed)}")
        if self.added:
            parts.append(f"新增了 {'、'.join(self.added)}")
        parts.append(
            f"总步行 {self.walking_before_m / 1000:.1f}km → {self.walking_after_m / 1000:.1f}km"
        )
        if self.budget_before is not None and self.budget_after is not None:
            parts.append(f"人均 ¥{self.budget_before:.0f} → ¥{self.budget_after:.0f}")
        return " · ".join(parts)


async def best_route_signature(
    db: AsyncSession, trip_id: uuid.UUID
) -> tuple[tuple[str, ...], int, Decimal | None]:
    """取一条 trip 的"首要方案"签名：站点名序列、步行米数、人均预算下限。"""
    route = (
        await db.execute(
            select(TripRoute).where(TripRoute.trip_id == trip_id).order_by(TripRoute.sort_order).limit(1)
        )
    ).scalar_one_or_none()
    if route is None:
        return ((), 0, None)
    names = tuple(
        str((row.place_snapshot or {}).get("name", ""))
        for row in (
            await db.execute(
                select(TripRouteStop)
                .where(TripRouteStop.trip_route_id == route.id)
                .order_by(TripRouteStop.seq)
            )
        ).scalars().all()
    )
    return names, int(route.walking_distance_m or 0), route.budget_min


def build_diff(
    before: tuple[tuple[str, ...], int, Decimal | None],
    after: tuple[tuple[str, ...], int, Decimal | None],
    *,
    days_before: int,
    days_after: int,
) -> DiffSummary:
    """比较两版行程的"首要方案"，产出可读差异（AC-8.4）。

    只比较首要方案（排序第一的那套）：用户改的是"当前看到的那条路线"，
    把所有方案的变化混在一句话里反而看不出来改了什么。
    """
    names_before, walking_before, budget_before = before
    names_after, walking_after, budget_after = after
    removed = tuple(name for name in names_before if name and name not in names_after)
    added = tuple(name for name in names_after if name and name not in names_before)
    return DiffSummary(
        removed=removed,
        added=added,
        walking_before_m=walking_before,
        walking_after_m=walking_after,
        budget_before=budget_before,
        budget_after=budget_after,
        route_count_before=len(names_before),
        route_count_after=len(names_after),
        days_before=days_before,
        days_after=days_after,
    )


async def record_revision(
    db: AsyncSession,
    *,
    trip: Trip,
    instruction: str,
    constraints: Sequence[Constraint],
    diff: DiffSummary,
    result_trip: Trip,
) -> TripRevision:
    revision = TripRevision(
        trip_id=trip.id,
        instruction=instruction,
        parsed_delta={
            "constraints": [
                {"type": c.type, "value": c.value, "raw": c.raw, "source": c.source} for c in constraints
            ]
        },
        result_trip_id=result_trip.id,
        diff_summary=diff.as_dict(),
        parse_source="rule",
    )
    db.add(revision)
    await db.flush()
    return revision


def build_revision_out(result_trip: Trip, diff: DiffSummary, *, clarification: str | None = None) -> RevisionOut:
    return RevisionOut(
        trip_id=str(result_trip.id),
        revision_no=result_trip.revision_no,
        diff=diff.as_dict(),
        needs_clarification=clarification,
    )


async def undo_last_revision(db: AsyncSession, trip: Trip) -> Trip:
    """回到上一版。⚠️ 必须先检查它属于当前 session（在路由层做归属校验）。

    ★ ``parent_trip_id`` 有两种含义，必须区分开 ★
    1. **上一版**：``source == "revision"`` 的行程，父节点是它取代的那一版；
    2. **来源（fork）**：复制出来的行程（``copied`` / ``plan_cache``）父节点指向
       被复制的那一条 trip —— 那可能是**别人**的行程。

    早期实现把两者混为一谈，并且试图"跳过" ``plan_cache`` 父节点继续往上找，
    结果是：复制别人的缓存 → 修改 → 撤销，会沿父链走进**原作者的行程**，
    再被归属校验拦下，用户拿到 403「这个行程不属于当前会话」；
    而复制出来的行程本身"撤销"不报"已是最早版本"，而是 403。

    副本是当前会话版本链的**起点**（它自己就是用户看到的第一版），不是中间的
    伪造记录。因此：``source`` 是复制来源时，它没有更早的版本。
    """
    if trip.parent_trip_id is None or (trip.source or "") in _COPY_SOURCES:
        raise AppError(
            ErrorCode.INVALID_INPUT,
            "这已经是最早的版本了，没有可以撤销的修改。",
            hint="继续修改会生成新的版本。",
            context={"trip_id": str(trip.id)},
        )
    return await load_trip(db, trip.parent_trip_id)


def _is_positive(value: Any) -> bool:
    """快照里的权重可能是 JSON 数字或字符串（历史数据）：统一按数值判断 > 0。"""
    try:
        return float(value) > 0
    except (TypeError, ValueError):
        return False


def plan_request_from_trip(raw: dict[str, Any], intent_snapshot: dict[str, Any]) -> PlanRequest:
    """还原出这次行程的规划入参，用于修改（revise）。

    两个来源，按可信度排序：
    1. ``TripRequest.raw_input`` —— 原始表单，最可信；
    2. ``intent_snapshot`` —— 复制来的行程（``raw_input`` 只有 copied_from）
       或历史数据，用意图快照反推。

    刻意用 ``model_validate`` 而不是手工拼字段：多出的字段会被 ``extra="forbid"``
    拦下，不会静默生成一个"看起来一样其实不同"的请求。
    """
    raw = raw or {}
    if "city" in raw:
        try:
            return PlanRequest.model_validate(raw)
        except ValidationError:
            # 旧数据可能缺字段：**回退到意图快照**，而不是直接把这次修改判为失败。
            # 这里刻意不吞掉所有异常（那样会把真正的 bug 也变成静默降级）。
            log.warning(
                "raw_input 无法解析，改用意图快照重建规划入参",
                extra={"event": "trip.raw_input_unparsable"},
            )

    budget = intent_snapshot.get("budget") or {}
    preferences = intent_snapshot.get("preferences") or {}
    return PlanRequest(
        city=str(intent_snapshot.get("city") or "guangzhou"),
        days=int(intent_snapshot.get("days") or 1),
        people=int(intent_snapshot.get("people") or 2),
        # 只带上**正权重**的偏好：权重 0 表示用户明确说"不要这一类"，
        # 把它写进 preferences 会被 ``build_intent`` 重新赋成 1.0，
        # 于是副本/修改反而开始"优先推荐"用户不想要的东西。
        preferences=[str(key) for key, weight in preferences.items() if _is_positive(weight)],
        pace=str(intent_snapshot.get("pace") or "relaxed"),
        budget=(
            None
            if budget.get("amount") in (None, "")
            else {"amount": Decimal(str(budget["amount"])), "scope": budget.get("scope", "per_person")}
        ),
        free_text="",
        start_time=intent_snapshot.get("start_time"),
        end_time=intent_snapshot.get("end_time"),
        travel_date=intent_snapshot.get("travel_date"),
    )
