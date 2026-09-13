/**
 * 规划进度流（SSE）的订阅封装。
 *
 * 为什么要单独一个模块：``EventSource`` 是"有副作用的对象"，
 * 直接写在组件里就没法测（jsdom 根本没有 EventSource）。
 * 这里把构造函数做成可注入的，组件传真的、测试传真假的 ——
 * 于是"事件解析 / 取消订阅 / 异常容错"都能被单测钉住。
 *
 * 两条纪律：
 * 1. **事件载荷是不可信输入**：解析失败就忽略这一条，绝不让一个畸形帧把整页打崩；
 * 2. **必须能取消**：组件卸载后继续收事件会 setState 到已卸载的组件上。
 */

import type { LlmStatus } from "@/lib/api";
import { API_BASE_URL } from "@/lib/api";

export interface PlanProgressEvent {
  stage: number;
  key: string;
  label: string;
  pct: number;
  detail: Record<string, unknown>;
}

export interface PlanCompletedEvent {
  trip_id: string;
  route_count: number;
  cached: boolean;
  degraded_modes: string[];
  llm: LlmStatus | null;
}

export interface PlanFailedEvent {
  code: string;
  message: string;
  hint: string;
}

export interface PlanStreamHandlers {
  onStarted?: (requestId: string | null) => void;
  onProgress?: (event: PlanProgressEvent) => void;
  onCompleted?: (event: PlanCompletedEvent) => void;
  onFailed?: (event: PlanFailedEvent) => void;
  /** 连接层错误（后端重启、代理掐断）。业务失败走 onFailed。 */
  onTransportError?: () => void;
}

/** 只需要 `addEventListener` / `close`，便于测试替身。 */
export interface EventSourceLike {
  addEventListener(type: string, listener: (event: MessageEvent) => void): void;
  close(): void;
}

export type EventSourceFactory = (url: string) => EventSourceLike;

export interface PlanStreamOptions {
  /** 测试注入用；默认用浏览器原生 EventSource。 */
  sourceFactory?: EventSourceFactory;
}

function defaultFactory(url: string): EventSourceLike {
  return new EventSource(url, { withCredentials: true }) as unknown as EventSourceLike;
}

/** 把相对路径拼成绝对地址（后端返回的是 `/api/v1/trips/{id}/stream`）。 */
export function absoluteStreamUrl(streamUrl: string): string {
  if (/^https?:\/\//i.test(streamUrl)) return streamUrl;
  return `${API_BASE_URL}${streamUrl.startsWith("/") ? "" : "/"}${streamUrl}`;
}

function parse<T>(event: MessageEvent): T | null {
  const raw = event.data;
  if (typeof raw !== "string") return null;
  try {
    return JSON.parse(raw) as T;
  } catch {
    // 畸形的帧被忽略而不是抛出：一条坏消息不该让用户看不到后面的好消息。
    return null;
  }
}

export function subscribeToPlan(
  streamUrl: string,
  handlers: PlanStreamHandlers,
  options: PlanStreamOptions = {},
): () => void {
  const factory =
    options.sourceFactory ??
    (typeof EventSource === "undefined" ? null : defaultFactory);
  if (factory === null) {
    // 非浏览器环境（SSR / 测试未注入工厂）：明确地什么都不做。
    return () => {};
  }

  const source = factory(absoluteStreamUrl(streamUrl));

  source.addEventListener("plan.started", (event) => {
    const payload = parse<{ request_id?: string }>(event);
    handlers.onStarted?.(payload?.request_id ?? null);
  });

  source.addEventListener("plan.progress", (event) => {
    const payload = parse<PlanProgressEvent>(event);
    if (payload) handlers.onProgress?.(payload);
  });

  source.addEventListener("plan.completed", (event) => {
    const payload = parse<PlanCompletedEvent>(event);
    if (payload) handlers.onCompleted?.(payload);
    source.close();
  });

  source.addEventListener("plan.failed", (event) => {
    const payload = parse<PlanFailedEvent>(event);
    if (payload) handlers.onFailed?.(payload);
    source.close();
  });

  let transportErrorReported = false;
  source.addEventListener("error", () => {
    // 连接层错误在 EventSource 里会**反复触发**（它会自动重连），
    // 所以这里只上报一次，由调用方决定是否重试。
    if (transportErrorReported) return;
    transportErrorReported = true;
    handlers.onTransportError?.();
  });

  return () => {
    source.close();
  };
}
