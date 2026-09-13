/**
 * 首页表单提交后的进度流展示测试。
 *
 * 表单提交拿到的是 202 + stream_url，所以"这次规划到底用了模型还是规则"
 * 只能从 SSE 的 `plan.completed` 里读。这里用假的 `subscribeToPlan` 把四条路径
 * 都走一遍：进度 → 完成（带 meta.llm）→ 失败 → 断流。
 *
 * 为什么必须测：这四段文案是用户唯一能看到的过程反馈，
 * 写错就等于"假装在跑"或"假装成功"。
 */

import { describe, expect, it, vi, beforeEach } from "vitest";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import type { PlanStreamHandlers } from "@/lib/plan-stream";
import { PlannerForm } from "@/components/planner-form";

const planTrip = vi.fn();
const subscribeToPlan = vi.fn();

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, planTrip: (...args: unknown[]) => planTrip(...args) };
});

vi.mock("@/lib/plan-stream", async () => {
  const actual =
    await vi.importActual<typeof import("@/lib/plan-stream")>("@/lib/plan-stream");
  return {
    ...actual,
    subscribeToPlan: (url: string, handlers: PlanStreamHandlers) =>
      subscribeToPlan(url, handlers),
  };
});

function handlers(): PlanStreamHandlers {
  const call = subscribeToPlan.mock.calls.at(-1);
  if (!call) throw new Error("组件没有订阅进度流");
  return call[1] as PlanStreamHandlers;
}

/** 事件是通过 EventSource 异步到达的，用 act 包住才能反映到断言上。 */
async function emit(run: (h: PlanStreamHandlers) => void) {
  await act(async () => {
    run(handlers());
  });
}

async function submitForm() {
  const user = userEvent.setup();
  render(<PlannerForm />);
  await user.click(screen.getByRole("button", { name: "开始规划" }));
  await waitFor(() => expect(subscribeToPlan).toHaveBeenCalled());
}

beforeEach(() => {
  planTrip.mockReset();
  subscribeToPlan.mockReset();
  subscribeToPlan.mockReturnValue(() => {});
  planTrip.mockResolvedValue({
    data: { request_id: "req-1", stream_url: "/api/v1/trips/req-1/stream" },
    meta: { request_id: "req-1", cached: false, cache_layer: null, degraded_modes: [], elapsed_ms: null },
  });
});

describe("提交后的进度流", () => {
  it("订阅后端给的 stream_url，并先显示等待态", async () => {
    await submitForm();
    expect(subscribeToPlan).toHaveBeenCalledWith(
      "/api/v1/trips/req-1/stream",
      expect.any(Object),
    );
    expect(screen.getByText(/正在等待规划结果/)).toBeInTheDocument();
  });

  it("收到 plan.progress 后显示阶段与百分比", async () => {
    await submitForm();
    await emit((h) =>
      h.onProgress?.({
        stage: 2,
        key: "candidates",
        label: "筛选 广州 1622 个地点",
        pct: 40,
        detail: {},
      }),
    );
    expect(await screen.findByText(/正在规划：筛选 广州 1622 个地点（40%）/)).toBeInTheDocument();
  });

  it("plan.completed 展示方案数、模型使用情况与降级模式", async () => {
    await submitForm();
    await emit((h) =>
      h.onCompleted?.({
        trip_id: "trip-1",
        route_count: 3,
        cached: false,
        degraded_modes: ["search:未配置搜索 API Key（只读本地知识库，不联网）"],
        llm: {
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
        },
      }),
    );

    expect(await screen.findByText(/已生成 3 套方案/)).toBeInTheDocument();
    expect(screen.getByTestId("llm-summary")).toBeInTheDocument();
    expect(screen.getByText(/deepseek-flash/)).toBeInTheDocument();
    expect(screen.getByText(/当前降级模式：search:/)).toBeInTheDocument();
    // 明确边界：完整路线卡片还没有
    expect(screen.getByText(/属于 M5/)).toBeInTheDocument();
  });

  it("命中缓存时如实说明未重新计算", async () => {
    await submitForm();
    await emit((h) =>
      h.onCompleted?.({
        trip_id: "trip-1",
        route_count: 2,
        cached: true,
        degraded_modes: [],
        llm: null,
      }),
    );
    expect(await screen.findByText(/命中缓存，未重新计算/)).toBeInTheDocument();
    expect(screen.getByText(/没有带上模型使用信息/)).toBeInTheDocument();
  });

  it("plan.failed 展示错误码与提示，而不是装作成功", async () => {
    await submitForm();
    await emit((h) =>
      h.onFailed?.({
        code: "NO_FEASIBLE_ROUTE",
        message: "在你给出的限制下排不出可行的路线",
        hint: "试着放宽预算、步行量或减少偏好/排除项。",
      }),
    );
    expect(await screen.findByText("规划没有完成")).toBeInTheDocument();
    expect(screen.getByText(/NO_FEASIBLE_ROUTE/)).toBeInTheDocument();
    expect(screen.getByText(/试着放宽预算/)).toBeInTheDocument();
    expect(screen.queryByText(/已生成/)).not.toBeInTheDocument();
  });

  it("断流时给出可重试的说明（且只提示一次）", async () => {
    await submitForm();
    await emit((h) => h.onTransportError?.());
    expect(
      await screen.findByText(/进度流已断开（后端可能重启了）/),
    ).toBeInTheDocument();
  });
});
