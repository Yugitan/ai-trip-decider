"use client";

import { useEffect, useState } from "react";

import { getHealth, type HealthData } from "@/lib/api";
import { lookupLabel } from "@/lib/format";

/**
 * 降级模式的可读文案。后端返回的是 `llm:missing` 这类机器码，
 * 前端负责翻译成人话 —— 用户看到的是「会怎样」，而不是内部代号。
 */
const DEGRADED_LABELS: Record<string, string> = {
  llm: "未配置 LLM：将使用规则引擎",
  search: "未配置搜索：仅用本地知识库",
  map: "地图：距离为路网或估算值",
  weather: "未配置天气：不校验天气影响",
  pricing: "价格未校准：预算为估算值",
};

export function describeDegradedMode(mode: string): string {
  const key = (mode.split(":")[0] ?? "").trim().toLowerCase();
  // 走原型安全查表：`DEGRADED_LABELS["constructor"]` 会拿到函数、
  // `["__proto__"]` 会拿到对象，直接 `??` 回退会把它们当文案交给 React 渲染而崩掉整页。
  return lookupLabel(DEGRADED_LABELS, key) ?? `降级模式：${mode}`;
}

type BannerState =
  | { kind: "loading" }
  | { kind: "connected"; health: HealthData }
  | { kind: "unreachable" }
  | { kind: "error"; code: string; status: number | null };

function isNetworkError(error: unknown): boolean {
  return (
    typeof error === "object" &&
    error !== null &&
    (error as { name?: unknown }).name === "NetworkError"
  );
}

function readApiError(error: unknown): { code: string; status: number | null } | null {
  if (typeof error !== "object" || error === null) return null;
  const candidate = error as { code?: unknown; status?: unknown };
  if (typeof candidate.code !== "string") return null;
  return {
    code: candidate.code,
    status: typeof candidate.status === "number" ? candidate.status : null,
  };
}

function formatCount(value: number | undefined): string {
  return typeof value === "number" ? String(value) : "—";
}

/**
 * 后端状态条。
 *
 * 关键约束：`getHealth()` 只在客户端 useEffect 里调用 —— 服务端渲染阶段
 * 绝不能发网络请求（`next build` 时后端并不在运行）。
 * 卸载后回调可能仍在飞行，用 cancelled 标志位兜住，避免卸载后 setState。
 */
export function DegradedBanner() {
  const [state, setState] = useState<BannerState>({ kind: "loading" });
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let cancelled = false;

    getHealth()
      .then((result) => {
        if (cancelled) return;
        setState({ kind: "connected", health: result.data });
      })
      .catch((error: unknown) => {
        if (cancelled) return;
        if (isNetworkError(error)) {
          setState({ kind: "unreachable" });
          return;
        }
        const apiError = readApiError(error);
        setState({
          kind: "error",
          code: apiError?.code ?? "UNKNOWN",
          status: apiError?.status ?? null,
        });
      });

    return () => {
      cancelled = true;
    };
  }, [attempt]);

  return (
    <div
      role="status"
      aria-live="polite"
      className="rounded-card border border-line bg-shell/70 px-4 py-3 text-xs leading-relaxed text-ink-soft"
    >
      {state.kind === "loading" ? (
        <p className="animate-stage">正在检查后端状态…</p>
      ) : null}

      {state.kind === "connected" ? (
        <ConnectedStatus health={state.health} />
      ) : null}

      {state.kind === "unreachable" ? (
        <div className="flex flex-wrap items-center justify-between gap-2">
          <p>
            <span className="font-medium text-ink">未连接到后端服务</span>
            <span className="mx-1.5 text-ink-faint">·</span>
            在项目根目录运行{" "}
            <code className="rounded bg-sand px-1.5 py-0.5 text-ink">
              make dev-backend
            </code>{" "}
            启动后端后再试
          </p>
          <RetryButton onClick={() => setAttempt((value) => value + 1)} />
        </div>
      ) : null}

      {state.kind === "error" ? (
        <div className="flex flex-wrap items-center justify-between gap-2">
          <p className="tnum">
            后端状态检查失败
            <span className="mx-1.5 text-ink-faint">·</span>
            {state.status === null ? "无 HTTP 状态" : `HTTP ${state.status}`}
            <span className="mx-1.5 text-ink-faint">·</span>
            {state.code}
          </p>
          <RetryButton onClick={() => setAttempt((value) => value + 1)} />
        </div>
      ) : null}
    </div>
  );
}

function ConnectedStatus({ health }: { health: HealthData }) {
  const database = health.database;
  const modes = health.degraded_modes ?? [];

  return (
    <div>
      <p className="tnum">
        后端已连接
        <span className="mx-1.5 text-ink-faint">·</span>
        知识库 {formatCount(database?.active_places)} 个地点 /{" "}
        {formatCount(database?.routes)} 条路线
        <span className="mx-1.5 text-ink-faint">·</span>
        版本 {health.version}
      </p>

      {health.status === "degraded" && modes.length === 0 ? (
        <p className="mt-2 rounded-[10px] border border-line bg-coral-tint px-2.5 py-2">
          服务处于降级状态，部分能力可能不可用。
        </p>
      ) : null}

      {modes.length > 0 ? (
        <div className="mt-2 rounded-[10px] border border-line bg-coral-tint px-2.5 py-2">
          <p className="font-medium text-ink">当前降级模式</p>
          <ul className="mt-1 space-y-0.5">
            {modes.map((mode) => (
              <li key={mode} className="flex gap-1.5">
                <span aria-hidden="true" className="text-ink-faint">
                  ·
                </span>
                <span>{describeDegradedMode(mode)}</span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </div>
  );
}

function RetryButton({ onClick }: { onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="inline-flex min-h-11 items-center rounded-full border border-line bg-shell px-4 text-xs text-ink-soft transition-colors duration-300 hover:border-ink/25 hover:text-ink"
    >
      重试
    </button>
  );
}
