import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import HomePage from "@/app/page";
import { NetworkError, getHealth } from "@/lib/api";

// 首页是服务端组件，但渲染它会带上客户端子组件（健康状态条 / 规划表单）。
// 这里把网络函数换掉：构建与测试都不允许真的去连后端。
vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    getHealth: vi.fn(),
    planTrip: vi.fn(),
  };
});

const getHealthMock = vi.mocked(getHealth);

describe("首页", () => {
  beforeEach(() => {
    getHealthMock.mockRejectedValue(new NetworkError(new Error("offline")));
  });

  it("Hero 文案与信任文案原样呈现，且 h1 唯一", async () => {
    render(<HomePage />);

    const headings = screen.getAllByRole("heading", { level: 1 });
    expect(headings).toHaveLength(1);
    expect(headings[0]).toHaveTextContent("你负责决定怎么玩");
    expect(headings[0]).toHaveTextContent("路线交给我。");

    expect(
      screen.getByText(
        "我替你研究、筛选、组合并验证路线，而不是丢给你一篇攻略。",
      ),
    ).toBeInTheDocument();

    expect(
      screen.getByText("200+ 广州地点 · 30+ 精选路线 · 真实距离校验 · 数据有来源"),
    ).toBeInTheDocument();

    // 后端没启动也不影响首页可用：状态条如实说明，表单照常可填
    expect(await screen.findByText("未连接到后端服务")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "开始规划" })).toBeEnabled();
  });

  it("展示 A / B / C 三套方案的差异说明", async () => {
    render(<HomePage />);
    // 等健康检查落地，避免状态更新落在 act() 之外
    await screen.findByText("未连接到后端服务");

    expect(
      screen.getByRole("heading", { name: "你会拿到 A / B / C 三套方案" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: "轻松休闲" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: "经典打卡" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "主题型" })).toBeInTheDocument();
  });

  it("规划卡在首页可达：跳到表单的链接 + 目的地默认广州", async () => {
    render(<HomePage />);
    await screen.findByText("未连接到后端服务");

    expect(screen.getByRole("link", { name: "跳到规划表单" })).toHaveAttribute(
      "href",
      "#planner",
    );
    expect(screen.getByLabelText("目的地")).toHaveValue("广州");
    expect(screen.getByRole("link", { name: "数据来源" })).toHaveAttribute(
      "href",
      "/about/data",
    );
  });
});
