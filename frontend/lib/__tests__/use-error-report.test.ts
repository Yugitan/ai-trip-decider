/**
 * 错误上报 hook 的测试（PRD FR-13.5）。
 *
 * ★ 为什么单独测它 ★
 * 两个 error boundary（`app/error.tsx` / `app/global-error.tsx`）共用这一段逻辑。
 * 各写一份的话，一边记得带 `digest`、另一边忘了 —— 而"忘了"只有在真的崩两次时才看得出来。
 * 这里钉住的是：**上报一次、带上 digest、失败不影响渲染**。
 */

import { renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { ClientErrorInput } from "@/lib/api";
import { useErrorReport } from "@/lib/use-error-report";

const reportMock = vi.fn<(input: ClientErrorInput) => Promise<boolean>>();

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, reportClientError: (input: ClientErrorInput) => reportMock(input) };
});

beforeEach(() => {
  reportMock.mockReset();
  reportMock.mockResolvedValue(true);
});

describe("useErrorReport", () => {
  it("挂载时上报一次，并带上组件名与 digest", () => {
    const error = Object.assign(new Error("渲染失败"), { digest: "abc123" });
    renderHook(() => useErrorReport("route-error-boundary", error));

    expect(reportMock).toHaveBeenCalledTimes(1);
    expect(reportMock).toHaveBeenCalledWith({
      component: "route-error-boundary",
      message: "渲染失败",
      code: "abc123",
      context: { digest: "abc123" },
    });
  });

  it("没有 digest 时上报 null 而不是空字符串（\"没有编号\"与\"编号是空\"是两件事）", () => {
    renderHook(() => useErrorReport("global-error-boundary", new Error("boom")));
    expect(reportMock).toHaveBeenCalledWith({
      component: "global-error-boundary",
      message: "boom",
      code: null,
      context: { digest: null },
    });
  });

  it("错误信息为空时给一句实在话（空消息在表里等于没写）", () => {
    renderHook(() => useErrorReport("route-error-boundary", new Error("")));
    expect(reportMock.mock.calls[0]?.[0].message).toBe("浏览器没有提供错误信息");
  });

  it("同一个错误对象重复渲染不会重复上报", () => {
    const error = new Error("boom");
    const { rerender } = renderHook(({ err }) => useErrorReport("c", err), {
      initialProps: { err: error },
    });
    rerender({ err: error });
    expect(reportMock).toHaveBeenCalledTimes(1);

    // 换了一个错误对象 = 一次新的现场，要再报一次
    rerender({ err: new Error("另一个错误") });
    expect(reportMock).toHaveBeenCalledTimes(2);
  });

  it("上报失败也不能抛出去（崩溃页里再抛一个异常只会更糟）", () => {
    reportMock.mockRejectedValue(new Error("网络断了"));
    expect(() => renderHook(() => useErrorReport("c", new Error("boom")))).not.toThrow();
  });
});
