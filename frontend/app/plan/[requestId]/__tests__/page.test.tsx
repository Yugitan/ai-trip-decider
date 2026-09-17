/**
 * `/plan/[requestId]` 生成页的测试（PRD §7.1 / §17.3）。
 *
 * 页面本身几乎只有布局，真正的内容在 `PlanProgress`（已由它自己的用例覆盖）。
 * 这里守的是**地址里的 request_id 真的被用上了**：
 * 一个把 id 丢掉、只渲染空壳的页面同样"能打开"，但后端那条进度流就再也没人订阅了。
 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import PlanPage from "@/app/plan/[requestId]/page";

const subscribeMock = vi.fn();

vi.mock("@/lib/plan-stream", async () => {
  const actual = await vi.importActual<typeof import("@/lib/plan-stream")>("@/lib/plan-stream");
  return { ...actual, subscribeToPlan: (url: string, handlers: unknown) => subscribeMock(url, handlers) };
});

describe("/plan/[requestId]", () => {
  it("把地址里的 request_id 交给进度视图（进度流订阅它）", async () => {
    subscribeMock.mockReset();
    subscribeMock.mockReturnValue(() => {});

    const element = await PlanPage({ params: Promise.resolve({ requestId: "req-xyz" }) });
    render(element);

    expect(screen.getByRole("heading", { name: "正在生成路线方案" })).toBeInTheDocument();
    expect(screen.getByText(/request_id：req-xyz/)).toBeInTheDocument();
    expect(subscribeMock).toHaveBeenCalledWith(
      "/api/v1/trips/req-xyz/stream",
      expect.anything(),
    );
    // 说明四个阶段来自后端真实进度，而不是动画
    expect(screen.getByText(/四个阶段都是后端真实进度/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /回到规划/ })).toHaveAttribute("href", "/#planner");
  });
});
