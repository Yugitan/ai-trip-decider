/**
 * 两个 error boundary 的测试（PRD FR-13.1 / FR-13.5）。
 *
 * ★ 这里守的是"无裸白屏" ★
 * 崩溃页必须同时做到两件事：给用户一句人话 + 一个可执行的下一步，
 * 并且把这次错误记下来（否则我们永远不知道用户撞上了什么）。
 *
 * `global-error` 用的是 `renderToStaticMarkup` 而不是 RTL 的容器：
 * 它的返回值是 `<html>`，塞进 RTL 默认的 `<div>` 里 React 会打一条
 * `In HTML, <html> cannot be a child of <div>` —— 测试照样通过，
 * 但每次运行都会在日志里留一条刺眼的警告，而长期忽略警告的团队
 * 最终会漏掉真正的 warning（TASKS 里已经吃过一次这个教训）。
 */

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";

import GlobalError from "@/app/global-error";
import RouteError from "@/app/error";

const reportMock = vi.fn<(input: { component: string }) => Promise<boolean>>();

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    reportClientError: (input: { component: string }) => reportMock(input),
  };
});

beforeEach(() => {
  reportMock.mockReset();
  reportMock.mockResolvedValue(true);
});

describe("RouteError（路由级）", () => {
  it("上报一次，并显示错误编号（用户报障时能念出来的东西）", () => {
    const error = Object.assign(new Error("渲染失败"), { digest: "digest-1" });
    render(<RouteError error={error} reset={() => {}} />);

    expect(reportMock).toHaveBeenCalledWith(expect.objectContaining({ component: "route-error-boundary" }));
    expect(screen.getByText(/错误编号：digest-1/)).toBeInTheDocument();
    expect(screen.getByText(/问题出在我们这边/)).toBeInTheDocument();
  });

  it("「重试」真的调用 reset（而不是刷新整页糊过去）", async () => {
    const reset = vi.fn();
    render(<RouteError error={new Error("boom")} reset={reset} />);
    await userEvent.click(screen.getByTestId("error-retry"));
    expect(reset).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("link", { name: "回到首页" })).toHaveAttribute("href", "/");
  });

  it("没有 digest 时不显示那一行（不打印一句空编号）", () => {
    render(<RouteError error={new Error("boom")} reset={() => {}} />);
    expect(screen.queryByText(/错误编号/)).not.toBeInTheDocument();
    // 原始信息仍然给出来：排查时这是唯一的第一手材料
    expect(screen.getByText(/原始信息（排查用）：boom/)).toBeInTheDocument();
  });
});

describe("GlobalError（根布局级）", () => {
  it("自带 <html>/<body> 骨架（布局本身可能就是炸掉的那一半）", () => {
    const html = renderToStaticMarkup(
      <GlobalError error={new Error("boom")} reset={() => {}} />,
    );
    expect(html).toContain("<html lang=\"zh-CN\">");
    expect(html).toContain("<body");
    expect(html).toContain("应用没能启动起来");
    expect(html).toContain("重试");
  });

  it("有 digest 时把编号写进页面，没有时不写", () => {
    const withDigest = renderToStaticMarkup(
      <GlobalError error={Object.assign(new Error("boom"), { digest: "g-1" })} reset={() => {}} />,
    );
    expect(withDigest).toContain("错误编号：g-1");

    const without = renderToStaticMarkup(<GlobalError error={new Error("boom")} reset={() => {}} />);
    expect(without).not.toContain("错误编号");
  });
});
