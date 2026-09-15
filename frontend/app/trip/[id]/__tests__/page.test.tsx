/**
 * 行程页外壳（`app/trip/[id]/page.tsx`）的测试。
 *
 * 这一页的价值全在**地址**上：`/trip/{id}` 要能刷新、能收藏、能复制给自己。
 * 因此这里钉住的是三件接线与一条边界：
 * 1. 地址里的 id 必须原样交给工作区（传错了就是"打开别人的行程"，后端会 403）；
 * 2. 必须以 `mode="page"` 挂载 —— 这一模式下改路线/撤销会把地址栏同步成新版，
 *    而内嵌模式不会（那一页还在填表单，替用户改地址是自作主张）；
 * 3. 页面上要说清楚"只有这个浏览器能打开"和"改完是新的一版"，别让用户自己撞 403；
 * 4. 头部不高亮"首页" —— `aria-current="page"` 指的是你真正所在的那一页。
 *
 * 工作区本身是客户端组件（要带会话 cookie 取行程），这里换成替身：
 * 这一页该负责的是"把它挂对"，不是重复测它内部的取数与操作。
 */

import { render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import TripPage from "@/app/trip/[id]/page";

vi.mock("@/components/trip-workspace", () => ({
  TripWorkspace: ({ tripId, mode }: { tripId: string; mode?: string }) => (
    <div data-testid="workspace" data-trip={tripId} data-mode={mode ?? "inline"} />
  ),
}));

/** 服务端组件是 async 的：直接 await 它再渲染返回的元素。 */
async function renderPage(id = "trip-abc") {
  const ui = await TripPage({ params: Promise.resolve({ id }) });
  render(ui);
}

describe("/trip/{id}", () => {
  it("★ 把地址里的 id 原样交给工作区，并以 page 模式挂载", async () => {
    await renderPage("11111111-2222-3333-4444-555555555555");

    const workspace = screen.getByTestId("workspace");
    expect(workspace).toHaveAttribute("data-trip", "11111111-2222-3333-4444-555555555555");
    // inline 模式不会同步地址栏 —— 挂错了这一页就"改完刷新回旧版"
    expect(workspace).toHaveAttribute("data-mode", "page");
  });

  it("★ 说清两个边界：只有这个浏览器能打开、改完是新的一版", async () => {
    await renderPage();

    expect(
      screen.getByRole("heading", { name: "这一版随时可以回来继续改" }),
    ).toBeInTheDocument();
    // 会话隔离：别人的浏览器打不开这个地址（后端 403），必须提前说
    expect(screen.getByText(/只在你这台浏览器里能打开/)).toBeInTheDocument();
    expect(screen.getByText(/按游客会话隔离/)).toBeInTheDocument();
    expect(screen.getByText(/用页面里的「分享这套路线」生成公开链接/)).toBeInTheDocument();
    // 版本语义：改一次就是另一条 trip，地址会跟着换
    expect(screen.getByText(/新的一版/)).toBeInTheDocument();
    expect(screen.getByText(/每条历史版本都保留/)).toBeInTheDocument();
  });

  it("★ 头部不高亮任何一项导航（你不是在首页）", async () => {
    await renderPage();

    const nav = screen.getByRole("navigation", { name: "主导航" });
    for (const link of within(nav).getAllByRole("link")) {
      expect(link).not.toHaveAttribute("aria-current");
    }
  });

  it("给走丢的人一条回得去的路", async () => {
    await renderPage();

    expect(screen.getByRole("link", { name: "首页重新规划" })).toHaveAttribute(
      "href",
      "/#planner",
    );
  });
});
