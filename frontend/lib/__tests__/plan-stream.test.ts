/**
 * SSE 订阅封装的单测。
 *
 * 守的几件事：四类事件都能解析、**畸形帧不会打崩页面**、
 * 完成/失败后要关闭连接（否则浏览器会一直重连）、取消订阅真的会断开。
 */

import { describe, expect, it, vi } from "vitest";

import { API_BASE_URL } from "@/lib/api";
import type { EventSourceLike } from "@/lib/plan-stream";
import { resolveStreamUrl, subscribeToPlan } from "@/lib/plan-stream";

class FakeEventSource implements EventSourceLike {
  readonly listeners = new Map<string, ((event: MessageEvent) => void)[]>();
  closed = false;

  addEventListener(type: string, listener: (event: MessageEvent) => void): void {
    const list = this.listeners.get(type) ?? [];
    list.push(listener);
    this.listeners.set(type, list);
  }

  close(): void {
    this.closed = true;
  }

  emit(type: string, data: unknown): void {
    const event = { data: typeof data === "string" ? data : JSON.stringify(data) } as MessageEvent;
    for (const listener of this.listeners.get(type) ?? []) listener(event);
  }
}

function setup() {
  const source = new FakeEventSource();
  const urls: string[] = [];
  return {
    source,
    urls,
    subscribe: (handlers: Parameters<typeof subscribeToPlan>[1]) =>
      subscribeToPlan("/api/v1/trips/abc/stream", handlers, {
        sourceFactory: (url) => {
          urls.push(url);
          return source;
        },
      }),
  };
}

const LLM = {
  enabled: true,
  used: true,
  provider: "deepseek",
  model: "deepseek-flash",
  prompt_version: "2026.09.2",
  calls: 2,
  cache_hits: 0,
  tokens_in: 771,
  tokens_out: 193,
  cost_cny: "0.004",
  cost_calibrated: true,
  tasks: { intent_patch: "llm", route_narrative: "llm" },
  fallback_reasons: [],
};

describe("subscribeToPlan", () => {
  it("相对 stream_url 默认保持同源（走 Next 的 /api 代理）", () => {
    // 默认 API_BASE_URL 是空串：同源请求才会带上第一方会话 cookie，
    // 而跨站的 Set-Cookie 会被浏览器当作第三方 cookie 丢掉（详见 lib/api.ts）。
    expect(API_BASE_URL).toBe("");
    expect(resolveStreamUrl("/api/v1/trips/x/stream")).toBe(
      "/api/v1/trips/x/stream",
    );
    // 后端如果哪天直接给绝对地址，就照用，不再拼一次
    expect(resolveStreamUrl("https://api.example.com/s")).toBe("https://api.example.com/s");
    // 没带前导斜杠的相对路径也能拼对
    expect(resolveStreamUrl("api/v1/x")).toBe("/api/v1/x");
  });

  it("解析进度与完成事件（含 meta.llm）", () => {
    const { source, subscribe } = setup();
    const onProgress = vi.fn();
    const onCompleted = vi.fn();

    subscribe({ onProgress, onCompleted });
    source.emit("plan.started", { request_id: "r-1" });
    source.emit("plan.progress", { stage: 2, key: "candidates", label: "筛选", pct: 40, detail: {} });
    source.emit("plan.completed", {
      trip_id: "t-1",
      route_count: 3,
      cached: false,
      degraded_modes: [],
      llm: LLM,
    });

    expect(onProgress).toHaveBeenCalledWith(
      expect.objectContaining({ key: "candidates", pct: 40 }),
    );
    expect(onCompleted).toHaveBeenCalledWith(
      expect.objectContaining({ trip_id: "t-1", route_count: 3 }),
    );
    expect(onCompleted.mock.calls[0]?.[0].llm?.model).toBe("deepseek-flash");
    // 完成后必须关流：否则浏览器会带着同一个 request_id 反复重连
    expect(source.closed).toBe(true);
  });

  it("失败事件带 code/message/hint 并关流", () => {
    const { source, subscribe } = setup();
    const onFailed = vi.fn();
    subscribe({ onFailed });

    source.emit("plan.failed", { code: "NO_FEASIBLE_ROUTE", message: "排不出路线", hint: "放宽预算" });

    expect(onFailed).toHaveBeenCalledWith(
      expect.objectContaining({ code: "NO_FEASIBLE_ROUTE", hint: "放宽预算" }),
    );
    expect(source.closed).toBe(true);
  });

  it("畸形帧被忽略，不打崩页面也不触发回调", () => {
    const { source, subscribe } = setup();
    const onCompleted = vi.fn();
    const onProgress = vi.fn();
    subscribe({ onCompleted, onProgress });

    source.emit("plan.progress", "{这不是 JSON");
    source.emit("plan.completed", "|||");

    expect(onProgress).not.toHaveBeenCalled();
    expect(onCompleted).not.toHaveBeenCalled();
  });

  it("传输错误只上报一次（EventSource 会反复触发 error）", () => {
    const { source, subscribe } = setup();
    const onTransportError = vi.fn();
    subscribe({ onTransportError });

    source.emit("error", {});
    source.emit("error", {});
    source.emit("error", {});

    expect(onTransportError).toHaveBeenCalledTimes(1);
  });

  it("取消订阅会关闭连接", () => {
    const { source, subscribe } = setup();
    const unsubscribe = subscribe({});
    unsubscribe();
    expect(source.closed).toBe(true);
  });
});
