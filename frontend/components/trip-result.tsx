"use client";

import { useEffect, useState } from "react";

import {
  ApiError,
  getTrip,
  NetworkError,
  type TripOut,
  type TripRoute,
  type TripStop,
} from "@/lib/api";
import { RouteCompare } from "@/components/route-compare";
import { RouteMap } from "@/components/route-map";
import {
  formatArchetype,
  formatBudget,
  formatDistance,
  formatDuration,
  formatTransit,
  formatTransport,
  lookupLabel,
} from "@/lib/format";

/**
 * 行程的**渲染层**：把一份 `TripOut` 画成能读的路线卡片（纯展示 + 取回行程的 hook）。
 *
 * 带操作的入口在 `trip-workspace.tsx`（改路线 / 撤销 / 分享），
 * 分享页（`app/t/[slug]`）直接复用这里的 `TripResultView`。
 *
 * 多套方案时，列表上方会先出现一张逐项对比表（`components/route-compare.tsx`）——
 * 卡片负责"每套方案讲完整自己"，表格负责"让三套方案可比"。两处用同一批格式化函数，
 * 所以同一个数字在表格里和卡片里长得一样。
 *
 * ★ 为什么必须再发一次请求 ★
 * `plan.completed` 这个 SSE 事件里只有**元信息**（方案数 / 是否命中缓存 / 模型用量 / trip_id），
 * 路线与站点从来不进事件流。所以此前的症状是：点「开始规划」→ 后端真的算了 3 套方案 →
 * 界面上却只有一句「已生成 3 套方案」，一条路线都看不到。**事件流负责进度，行程要单独取。**
 *
 * ★ 三条渲染纪律（与后端 `trip_service` / `feasibility` 的边界一一对应）★
 * 1. **估算值必须和数字一起显示**：`transport_source === "estimated"` 的每一段都要带「估算值」，
 *    `budget_estimated` 为真时要标出来 —— 不可信的数字比没有数字更危险；
 * 2. **不知道就说不知道**：缺失的时长/距离/票价交给 `lib/format`，它返回「未知」而不是 0；
 * 3. **不补内容**：方案少于 3 套就照实说（`route_count_note`），没有路线就明说没有，
 *    绝不在前端凑一份"看起来像真的"行程。
 */

/** 站点级校验码 → 人话。码是给机器看的，这一层负责翻译。 */
export const STOP_WARNING_LABELS: Readonly<Record<string, string>> = {
  HOURS_UNKNOWN: "营业时间未知，出发前请确认",
  HOURS_NOT_CHECKED: "未给出出行日期，营业时间未核对",
  PRICE_UNKNOWN: "票价未知",
  DATA_CONFLICTING: "信息源之间有冲突",
  ESTIMATED_TRANSIT: "耗时与距离为估算值",
};

/** 未知码**原样返回**：后端新增一个校验码时，用户看到乱码好过看到一个编好的说法。 */
export function formatStopWarning(code: string): string {
  return lookupLabel(STOP_WARNING_LABELS, code) ?? code;
}

function StopRow({ stop }: { stop: TripStop }) {
  const warnings = stop.warnings ?? [];
  const estimated = stop.transport_source === "estimated";

  return (
    <li className="flex flex-wrap items-baseline gap-x-2.5 gap-y-1 text-sm">
      <span className="tnum shrink-0 text-xs text-ink-faint">
        {stop.arrive_time}–{stop.depart_time}
      </span>
      <span className="font-medium text-ink">{stop.name}</span>
      <span className="tnum text-xs text-ink-faint">
        停留 {stop.stay_min} 分钟
      </span>
      {stop.why_recommended ? (
        <span className="rounded-full bg-teal-tint px-2 py-0.5 text-[11px] text-teal-dark">
          {stop.why_recommended}
        </span>
      ) : null}
      {stop.transport_mode === null ? null : (
        <span className="tnum text-xs text-ink-soft">
          → {formatTransport(stop.transport_mode)}
          {stop.transport_min === null ? "" : ` ${stop.transport_min} 分`}
          {stop.transport_distance_m === null
            ? ""
            : ` · ${formatDistance(stop.transport_distance_m)}`}
          {estimated ? (
            // 估算的通勤时间不能光给数字：PRD §23.2 第 31 条要求它必须带估算标记
            <span className="text-coral"> （估算值）</span>
          ) : null}
        </span>
      )}
      {warnings.length > 0 ? (
        <span className="text-[11px] text-coral">
          {warnings.map(formatStopWarning).join(" · ")}
        </span>
      ) : null}
    </li>
  );
}

function Badge({ label, title }: { label: string; title?: string }) {
  return (
    <span
      className="inline-flex items-center gap-1 rounded-full border border-coral/20 bg-coral-tint px-2.5 py-1 text-[11px] text-coral"
      title={title}
    >
      <span aria-hidden="true">⚠</span>
      {label}
    </span>
  );
}

export function TripRouteCard({ route }: { route: TripRoute }) {
  const violations = route.feasibility?.violations ?? [];
  // `at_seq` 为 null = 整条路线的问题（如"有项目没有价格"）；不为 null = 某个站点的问题，
  // 已经在站点行上标过了。分开渲染，同一件事才不会说两遍。
  const routeWideWarnings = (route.feasibility?.warnings ?? []).filter(
    (warning) => warning.at_seq === null || warning.at_seq === undefined,
  );
  const unknownItems = route.budget_unknown_items ?? [];

  return (
    <li
      data-testid={`trip-route-${route.label}`}
      className="rounded-[10px] border border-line bg-shell p-4"
    >
      <div className="flex flex-wrap items-center gap-2">
        <span className="tnum rounded-full border border-teal/25 bg-teal-tint px-2.5 py-0.5 text-xs text-teal-dark">
          方案 {route.label}
        </span>
        <span className="text-[11px] text-ink-faint">
          {formatArchetype(route.archetype)}
        </span>
      </div>

      <h4 className="mt-2.5 text-base font-medium text-ink">{route.name}</h4>

      {/* one_liner 里的站数/时长/步行距离是**代码算的**，与下面的模型文案严格分开 */}
      {route.one_liner ? (
        <p className="tnum mt-1 text-xs text-ink-soft">{route.one_liner}</p>
      ) : null}

      {route.recommendation_reason ? (
        <p className="mt-2 text-sm leading-relaxed text-ink-soft">
          {route.recommendation_reason}
        </p>
      ) : null}

      <dl className="mt-3 flex flex-wrap gap-x-6 gap-y-1.5 text-xs">
        <div className="flex gap-1.5">
          <dt className="text-ink-faint">总时长</dt>
          <dd className="tnum text-ink-soft">
            {formatDuration(route.total_duration_min)}
          </dd>
        </div>
        <div className="flex gap-1.5">
          <dt className="text-ink-faint">步行</dt>
          <dd className="tnum text-ink-soft">
            {formatDistance(route.walking_distance_m)}
          </dd>
        </div>
        <div className="flex gap-1.5">
          <dt className="text-ink-faint">交通</dt>
          <dd className="tnum text-ink-soft">
            {formatTransit(route.transit_time_min, route.transit_distance_m)}
          </dd>
        </div>
        <div className="flex gap-1.5">
          <dt className="text-ink-faint">预算</dt>
          <dd className="tnum text-ink-soft">
            {formatBudget(route.budget_min, route.budget_max, route.budget_scope)}
            {route.budget_estimated === true ? (
              <span className="text-coral"> （估算）</span>
            ) : null}
          </dd>
        </div>
        <div className="flex gap-1.5">
          <dt className="text-ink-faint">地点</dt>
          <dd className="tnum text-ink-soft">{route.place_count} 个</dd>
        </div>
      </dl>

      {/* 地图画不出自己的三种情况（缺 Key / 加载失败 / 坐标不全）都会在卡片里说明，
          站点列表不受影响 —— 所以这里总是渲染，由它自己决定画什么。 */}
      <RouteMap
        label={route.label}
        points={route.stops.map((stop) => ({
          seq: stop.seq,
          name: stop.name,
          latitude: stop.latitude,
          longitude: stop.longitude,
        }))}
      />

      {route.stops.length > 0 ? (
        <ol className="mt-3 space-y-2 border-t border-line pt-3">
          {route.stops.map((stop) => (
            <StopRow key={`${stop.seq}-${stop.place_id}`} stop={stop} />
          ))}
        </ol>
      ) : (
        <p className="mt-3 border-t border-line pt-3 text-xs leading-relaxed text-ink-soft">
          这套方案没有带站点明细 —— 后端返回的就是空的，界面不会替它编几个出来。
        </p>
      )}

      {route.best_for && route.best_for.length > 0 ? (
        <p className="mt-3 text-xs text-ink-soft">
          适合：{route.best_for.join(" · ")}
        </p>
      ) : null}

      {route.pros && route.pros.length > 0 ? (
        <ul className="mt-3 space-y-1">
          {route.pros.map((pro) => (
            <li key={pro} className="text-xs leading-relaxed text-teal-dark">
              优点 · {pro}
            </li>
          ))}
        </ul>
      ) : null}

      {route.cons && route.cons.length > 0 ? (
        <ul className="mt-2 space-y-1">
          {route.cons.map((con) => (
            <li key={con} className="text-xs leading-relaxed text-ink-soft">
              需要留意 · {con}
            </li>
          ))}
        </ul>
      ) : null}

      {violations.length > 0 ? (
        <ul className="mt-3 space-y-1 rounded-[10px] border border-coral/40 bg-coral-tint/70 px-3 py-2">
          {violations.map((violation, index) => (
            <li
              key={`${violation.code ?? "VIOLATION"}-${index}`}
              className="text-xs leading-relaxed text-ink-soft"
            >
              未通过校验：{violation.message ?? violation.code ?? "未知问题"}
            </li>
          ))}
        </ul>
      ) : null}

      {routeWideWarnings.length > 0 ? (
        <ul className="mt-2 space-y-1">
          {routeWideWarnings.map((warning, index) => (
            <li
              key={`${warning.code ?? "WARNING"}-${index}`}
              className="text-xs leading-relaxed text-ink-soft"
            >
              整条路线：{warning.message ?? warning.code ?? "未知提示"}
            </li>
          ))}
        </ul>
      ) : null}

      {route.budget_estimated === true || unknownItems.length > 0 ? (
        <div className="mt-3 flex flex-wrap items-center gap-2 border-t border-line/70 pt-3">
          {route.budget_estimated === true ? (
            <Badge
              label="预算含估算值"
              title="部分项目没有可核验的价格，金额仅供参考"
            />
          ) : null}
          {unknownItems.length > 0 ? (
            <span className="text-[11px] leading-relaxed text-ink-faint">
              未含价格：{unknownItems.join("、")}
            </span>
          ) : null}
        </div>
      ) : null}
    </li>
  );
}

/** 纯渲染：给它一份行程，它就只画这一份行程。可单独单测。 */
export function TripResultView({ trip }: { trip: TripOut }) {
  return (
    <div
      data-testid="trip-result"
      className="rounded-[10px] border border-line bg-shell/60 p-4 sm:p-5"
    >
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h3 className="text-sm font-semibold text-ink">
          {trip.title ?? `${trip.days} 天行程`}
        </h3>
        <p className="tnum text-xs text-ink-faint">
          {trip.days} 天 · {trip.route_count} 套方案
          {trip.revision_no > 1 ? ` · 第 ${trip.revision_no} 版` : ""}
        </p>
      </div>

      {trip.route_count_note ? (
        <p className="mt-2 text-xs leading-relaxed text-coral">
          {trip.route_count_note}
        </p>
      ) : null}

      {trip.degraded_modes.length > 0 ? (
        <p className="mt-2 text-xs leading-relaxed text-ink-soft">
          当前降级模式：{trip.degraded_modes.join("；")}
        </p>
      ) : null}

      {trip.routes.length === 0 ? (
        <p className="mt-3 text-xs leading-relaxed text-ink-soft">
          这次没有生成任何方案。后端返回的路线列表是空的，界面不会先给你一份假的行程 ——
          可以调整偏好或放宽限制后重新提交。
        </p>
      ) : (
        <>
          {/* 先对比、再看细节：卡片把每套方案讲完整，表格让它们可比 */}
          <RouteCompare routes={trip.routes} />
          <ol className="mt-4 space-y-4">
            {trip.routes.map((route) => (
              <TripRouteCard key={route.id} route={route} />
            ))}
          </ol>
        </>
      )}

    </div>
  );
}

/** 界面上展示一次失败需要的四行信息。 */
export interface FailureInfo {
  title: string;
  message: string;
  hint: string | null;
  detail: string | null;
}

/**
 * 把异常翻译成给用户看的四行。区分「连不上」与「后端拒绝了」，别把排障引错方向。
 *
 * `title` 由调用方给：取回行程 / 改路线 / 撤销 / 分享，每个动作的说法都不一样，
 * 写死在这里就会变成"撤销失败了，但标题说取回失败"。
 */
export function describeFailure(error: unknown, title: string): FailureInfo {
  if (error instanceof NetworkError) {
    return {
      title,
      message: "没能连上后端服务。",
      hint: "确认后端还在运行（make dev-backend）后重试。",
      detail: null,
    };
  }
  if (error instanceof ApiError) {
    return {
      title,
      // 后端已经把话说清楚了（如 403「这个行程不属于当前会话」、
      // 422「这已经是最早的版本了」）—— 如实转述，不自己编一个理由。
      message: error.message,
      hint: error.hint === "" ? null : error.hint,
      detail: [
        `HTTP ${error.status}`,
        `错误码 ${error.code}`,
        error.requestId === null ? null : `request_id ${error.requestId}`,
      ]
        .filter((part) => part !== null)
        .join(" · "),
    };
  }
  return {
    title,
    message: "发生了未预期的错误。",
    hint: "可以重试。",
    detail: null,
  };
}

/**
 * 失败块：标题 / 说明 / 细节 / 提示，四行都给出来，不吞信息。
 *
 * 放在这里而不是各个组件里：结果面板、复制行程、行程页面失败时显示的是**同一件事**，
 * 三份拷贝迟早会漂移成三种说法（而"错误怎么呈现"恰恰是这个项目最不该走样的一块）。
 */
export function FailureBlock({ failure }: { failure: FailureInfo }) {
  return (
    <>
      <p className="text-sm font-semibold text-ink">{failure.title}</p>
      <p className="mt-1.5 text-xs leading-relaxed text-ink-soft">{failure.message}</p>
      {failure.detail === null ? null : (
        <p className="tnum mt-1 text-xs text-ink-soft">{failure.detail}</p>
      )}
      {failure.hint === null ? null : (
        <p className="mt-1 text-xs leading-relaxed text-ink-soft">{failure.hint}</p>
      )}
    </>
  );
}

/** 取回行程的三个状态 + 重试。 */
export interface TripLoadState {
  trip: TripOut | null;
  loading: boolean;
  error: unknown;
  reload: () => void;
}

/**
 * 按 `tripId` 取一份行程。
 *
 * 为什么不把状态放进组件里：取回行程这件事有**两个**使用方
 * （结果面板 `TripWorkspace`、以及刷新后重新加载同一条 trip），
 * 各自写一份 effect 就意味着两份取消逻辑、两份重试逻辑会慢慢漂移。
 *
 * `attempt` 只为了让「重试」能重新触发 effect —— 比在 effect 里手写一套
 * "重试中/已重试"状态机更不容易出错（也就不会出现"重试了但界面还停在旧结果"）。
 */
export function useTrip(tripId: string): TripLoadState {
  const [attempt, setAttempt] = useState(0);
  const [trip, setTrip] = useState<TripOut | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    // 换了 trip_id（改路线/撤销后是新的一条）就先清空：
    // 否则用户会有一段时间看着上一版的路线配着新版的按钮。
    setTrip(null);

    void (async () => {
      try {
        const result = await getTrip(tripId);
        // 组件已卸载（或又换了一份行程）时不再 setState
        if (cancelled) return;
        setTrip(result.data);
      } catch (cause: unknown) {
        if (cancelled) return;
        setError(cause);
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [tripId, attempt]);

  return { trip, loading, error, reload: () => setAttempt((current) => current + 1) };
}
