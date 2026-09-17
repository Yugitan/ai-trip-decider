/**
 * 生成过程视图的测试（PRD §5.3 / §7.1）。
 *
 * ★ 这里守的是\"进度不撒谎\" ★
 * 三个阶段只有后端的 `plan.progress` 事件能让它前进；
 * 没有事件时它必须停在\"等待\"，而不是自己跑一段动画。
 * 同样的道理适用于超时提示：到点说的是\"正在深度校验\"，
 * 而不是换一个更好看的转圈。
 */

import { render, screen, waitFor } from "@testing-library/react";
import { act } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { PlanProgress, describeDetail } from "@/components/plan-progress";
import type { PlanStreamHandlers } from "@/lib/plan-stream";

const subscribeMock = vi.fn<(url: string, handlers: PlanStreamHandlers) => () => void>();

vi.mock("@/lib/plan-stream", async () => {
  const actual =
    await vi.importActual<typeof import("@/lib/plan-stream")>("@/lib/plan-stream");
  return {
    ...actual,
    subscribeToPlan: (url: string, handlers: PlanStreamHandlers) => subscribeMock(url, handlers),
  };
});

function handlersOf(): PlanStreamHandlers {
  const call = subscribeMock.mock.calls[0];
  if (call === undefined) throw new Error("没有订阅进度流");
  return call[1];
}

beforeEach(() => {
  subscribeMock.mockReset();
  subscribeMock.mockReturnValue(() => {});
});

afterEach(() => {
  vi.useRealTimers();
});

describe("describeDetail", () => {
  it("只说自己认识的键，不认识的一律不说", () => {
    expect(describeDetail({ candidates: 47 })).toBe("候选 47 个");
    expect(describeDetail({ checked: 12, rejected: 4 })).toBe("已校验 12 条 · 剔除 4 条");
    expect(describeDetail({ reasons: [{ code: "time_conflict" }, { code: "detour" }] })).toBe(
      "原因：time_conflict、detour",
    );
    // 未知键 / 非数字 / 空对象 → 不说话（编一句\"已筛选 N 个\"很容易，但那是替后端说话）
    expect(describeDetail({ something: 1 })).toBeNull();
    expect(describeDetail({ candidates: "很多" })).toBeNull();
    expect(describeDetail({})).toBeNull();
    expect(describeDetail({ reasons: ["不是对象"] })).toBeNull();
  });
});

describe("PlanProgress", () => {
  it("订阅后端给的 stream_url，并先显示\"等待\"而不是假装在跑", () => {
    render(<PlanProgress requestId="req-1" />);

    expect(subscribeMock).toHaveBeenCalledTimes(1);
    expect(subscribeMock.mock.calls[0]?.[0]).toBe("/api/v1/trips/req-1/stream");
    expect(screen.getByText(/request_id：req-1/)).toBeInTheDocument();
    for (const key of ["intent", "candidates", "validate", "compose"]) {
      expect(screen.getByTestId(`plan-stage-${key}`).dataset.state).toBe("waiting");
    }
  });

  it("阶段按事件推进：已过去的打勾、当前的高亮、后面的等待", async () => {
    render(<PlanProgress requestId="req-1" />);
    act(() => {
      handlersOf().onProgress?.({
        stage: 2,
        key: "candidates",
        label: "筛选广州 212 个地点",
        pct: 40,
        detail: { candidates: 47 },
      });
    });

    await waitFor(() =>
      expect(screen.getByTestId("plan-stage-intent").dataset.state).toBe("done"),
    );
    const active = screen.getByTestId("plan-stage-candidates");
    expect(active.dataset.state).toBe("active");
    // 活动阶段用**后端给的文案**（它比我们的占位更具体），并带上真实百分比
    expect(active.textContent).toContain("筛选广州 212 个地点");
    expect(active.textContent).toContain("40%");
    expect(active.textContent).toContain("候选 47 个");
    expect(screen.getByTestId("plan-stage-validate").dataset.state).toBe("waiting");
  });

  it("完成时给出方案数、模型用量与结果页入口", async () => {
    render(<PlanProgress requestId="req-1" />);
    act(() => {
      handlersOf().onCompleted?.({
        trip_id: "trip-9",
        route_count: 3,
        cached: true,
        degraded_modes: ["map:estimated"],
        llm: null,
      });
    });

    const completed = await screen.findByTestId("plan-completed");
    expect(completed.textContent).toContain("已生成 3 套方案");
    expect(completed.textContent).toContain("命中缓存，未重新计算");
    expect(completed.textContent).toContain("降级模式：map:estimated");
    // meta.llm 缺失时也如实说（不是留空）
    expect(screen.getByText(/没有带上模型使用信息/)).toBeInTheDocument();
    expect(screen.getByTestId("plan-open-trip")).toHaveAttribute("href", "/trip/trip-9");
  });

  it("失败时给人话、提示与错误码，并指向\"改一下需求再试\"", async () => {
    render(<PlanProgress requestId="req-1" />);
    act(() => {
      handlersOf().onFailed?.({
        code: "NO_FEASIBLE_ROUTE",
        message: "在你给出的限制下排不出可行的路线",
        hint: "试着放宽预算或步行量。",
      });
    });

    const failed = await screen.findByTestId("plan-failed");
    expect(failed.textContent).toContain("在你给出的限制下排不出可行的路线");
    expect(failed.textContent).toContain("试着放宽预算或步行量。");
    expect(failed.textContent).toContain("NO_FEASIBLE_ROUTE");
    expect(screen.getByRole("link", { name: "改一下需求再试" })).toHaveAttribute(
      "href",
      "/#planner",
    );
  });

  it("★ 进度流断开时说\"可能重启了\"并给出可执行动作，不谎称还在跑 ★", async () => {
    render(<PlanProgress requestId="req-1" />);
    act(() => {
      handlersOf().onTransportError?.();
    });

    const note = await screen.findByText(/进度流已断开/);
    expect(note.textContent).toContain("刷新");
  });

  it("超时提示说的是实话：8 秒说深度校验、20 秒建议刷新", async () => {
    vi.useFakeTimers();
    render(<PlanProgress requestId="req-1" />);

    expect(screen.queryByTestId("plan-deep-check-note")).not.toBeInTheDocument();
    await act(async () => {
      vi.advanceTimersByTime(8_000);
    });
    expect(screen.getByTestId("plan-deep-check-note")).toBeInTheDocument();

    await act(async () => {
      vi.advanceTimersByTime(12_000);
    });
    expect(screen.queryByTestId("plan-deep-check-note")).not.toBeInTheDocument();
    expect(screen.getByTestId("plan-slow-note").textContent).toContain("刷新");
    // 计时文案必须带上秒数：\"已等待 20 秒\"与\"正在处理\"是两句完全不同的话
    expect(screen.getByText(/已等待 20 秒/)).toBeInTheDocument();
  });
});
