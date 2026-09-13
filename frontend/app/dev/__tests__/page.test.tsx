/**
 * 开发设置页（`app/dev/page.tsx`）的渲染测试。
 *
 * 这一页的"正确"很大程度是**不要泄露给用户**，而这类要求最容易在后续改动里悄悄失效
 * （比如有人顺手在主导航里加了个链接、或者去掉了 noindex）。所以这里钉住三件事：
 *
 * 1. 页面本身可用，且写明它是开发工具；
 * 2. 头部只在开发构建显示入口（`NODE_ENV !== "production"`）；
 * 3. 页面元数据禁止被搜索引擎收录。
 */

import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import DevSettingsPage, { metadata } from "@/app/dev/page";
import { NetworkError } from "@/lib/api";

const fetchDevConfig = vi.fn();

vi.mock("@/lib/dev-api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/dev-api")>("@/lib/dev-api");
  return { ...actual, fetchDevConfig: () => fetchDevConfig() };
});

beforeEach(() => {
  fetchDevConfig.mockReset();
  fetchDevConfig.mockRejectedValue(new NetworkError(new Error("offline")));
});

describe("开发设置页", () => {
  it("渲染面板骨架与返回入口，并说明这是开发工具", async () => {
    render(<DevSettingsPage />);

    expect(screen.getByRole("heading", { name: "开发设置" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /回到规划/ })).toHaveAttribute("href", "/");
    expect(screen.getByText(/这是开发期的配置面板，不是用户功能/)).toBeInTheDocument();
    // 后端不可用时也要给出原因，而不是空白面板
    expect(await screen.findByRole("status")).toHaveTextContent(/无法连接到规划服务/);
  });

  it("头部把「开发设置」标成当前页（开发构建下才渲染）", async () => {
    render(<DevSettingsPage />);
    // 等加载失败的状态落定，否则拒绝的 promise 会在用例结束后 setState
    await screen.findByRole("status");

    const entry = screen.getByRole("link", { name: "开发设置" });
    expect(entry).toHaveAttribute("href", "/dev");
    expect(entry).toHaveAttribute("aria-current", "page");
  });

  it("★ 不允许搜索引擎收录这个开发工具页", () => {
    expect(metadata.robots).toMatchObject({ index: false, follow: false });
  });
});
