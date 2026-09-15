/**
 * 分享页的「复制这套路线」（`components/copy-trip-button.tsx`）。
 *
 * 这条路径在分享页的测试里是看不到的 —— 那一页是只读快照，而这个按钮是**唯一**
 * 从"别人的行程"走到"我自己的行程"的入口（后端 `POST /public/trips/{slug}/copy`）。
 * 要钉住的三件事：
 * 1. 复制成功必须**带去 `/trip/{新 id}`** —— 只弹一句"复制成功"等于没给副本，
 *    用户仍然只在只读页面上；
 * 2. 失败时如实转述后端的话（分享被取消是 404 `SHARE_NOT_FOUND`，不是"网络错误"），
 *    而且**不能跳转** —— 跳到一个不存在的行程比停在原地更糟；
 * 3. 文案必须说清楚"复制出来的是属于你这份浏览器的副本，原行程不受影响"，
 *    否则用户会以为自己在改分享者的那一份。
 */

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, NetworkError, copyPublicTrip, type TripOut } from "@/lib/api";
import { CopyTripButton } from "@/components/copy-trip-button";

const { pushMock } = vi.hoisted(() => ({ pushMock: vi.fn() }));

// 组件只用到 `useRouter().push` —— 给一个最小替身，别把整个 App Router 拉进单测。
vi.mock("next/navigation", () => ({ useRouter: () => ({ push: pushMock }) }));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, copyPublicTrip: vi.fn() };
});

const copyMock = vi.mocked(copyPublicTrip);

const META = {
  request_id: "req-1",
  cached: false,
  cache_layer: null,
  degraded_modes: [],
  elapsed_ms: 5,
};

function copiedTrip(tripId: string): TripOut {
  return {
    trip_id: tripId,
    request_id: "req-1",
    city: "guangzhou",
    title: "广州 · 09:00–21:00",
    days: 1,
    revision_no: 1,
    route_count: 1,
    route_count_requested: 3,
    route_count_note: null,
    intent: {},
    degraded_modes: [],
    total_cost_cny: "0",
    generation_ms: 12,
    created_at: null,
    is_public: false,
    share_slug: null,
    routes: [],
  };
}

beforeEach(() => {
  copyMock.mockReset();
  pushMock.mockReset();
});

describe("复制这套路线", () => {
  it("★ 复制成功 → 带着新 trip_id 去它自己的页面", async () => {
    copyMock.mockResolvedValue({ data: copiedTrip("trip-copy-1"), meta: META });

    const user = userEvent.setup();
    render(<CopyTripButton slug="abc123456789" />);

    await user.click(screen.getByRole("button", { name: "复制这套路线" }));

    expect(copyMock).toHaveBeenCalledWith("abc123456789");
    expect(pushMock).toHaveBeenCalledWith("/trip/trip-copy-1");
    expect(await screen.findByRole("button", { name: "已复制" })).toBeInTheDocument();
  });

  it("复制中给出可见反馈并禁用按钮（不重复下单）", async () => {
    type CopyResult = Awaited<ReturnType<typeof copyPublicTrip>>;
    // 手动控制的 Promise：先把请求挂在"进行中"，看清按钮的状态再放行。
    let settle: (value: CopyResult) => void = () => {};
    copyMock.mockReturnValue(
      new Promise<CopyResult>((resolve) => {
        settle = (value) => resolve(value);
      }),
    );

    const user = userEvent.setup();
    render(<CopyTripButton slug="abc" />);
    await user.click(screen.getByRole("button", { name: "复制这套路线" }));

    const pending = screen.getByRole("button", { name: "正在复制…" });
    expect(pending).toBeDisabled();
    expect(pending).toHaveAttribute("aria-busy", "true");

    settle({ data: copiedTrip("trip-copy-9"), meta: META });
    expect(await screen.findByRole("button", { name: "已复制" })).toBeInTheDocument();
    expect(copyMock).toHaveBeenCalledTimes(1);
  });

  it("★ 分享已取消（404）时如实转述后端说法，且不跳转", async () => {
    copyMock.mockRejectedValue(
      new ApiError(
        {
          code: "SHARE_NOT_FOUND",
          message: "分享链接不存在或已失效",
          hint: "请向分享者再要一个链接。",
          context: {},
        },
        404,
        "req-404",
      ),
    );

    const user = userEvent.setup();
    render(<CopyTripButton slug="gone" />);
    await user.click(screen.getByRole("button", { name: "复制这套路线" }));

    const block = await screen.findByTestId("copy-error");
    expect(block).toHaveTextContent("没能复制这套路线");
    expect(block).toHaveTextContent("分享链接不存在或已失效");
    expect(block).toHaveTextContent("错误码 SHARE_NOT_FOUND");
    expect(block).toHaveTextContent(/HTTP 404/);
    expect(block).toHaveTextContent(/再要一个链接/);
    // ★ 不能跳到一个不存在的行程
    expect(pushMock).not.toHaveBeenCalled();
    // 失败后按钮要能再点（而不是卡在"正在复制…"）
    expect(screen.getByRole("button", { name: "复制这套路线" })).toBeEnabled();
  });

  it("连不上后端时提示启动后端，而不是说链接失效", async () => {
    copyMock.mockRejectedValue(new NetworkError(new Error("offline")));

    const user = userEvent.setup();
    render(<CopyTripButton slug="abc" />);
    await user.click(screen.getByRole("button", { name: "复制这套路线" }));

    const block = await screen.findByTestId("copy-error");
    expect(block).toHaveTextContent("没能连上后端服务。");
    expect(block).toHaveTextContent(/make dev-backend/);
    expect(pushMock).not.toHaveBeenCalled();
  });

  it("★ 界面上先说清楚：副本归你、原行程不受影响", () => {
    render(<CopyTripButton slug="abc" />);

    expect(screen.getByText(/属于你这个浏览器/)).toBeInTheDocument();
    expect(screen.getByText(/分享者的那一份不会变/)).toBeInTheDocument();
    // 不需要登录这件事也要说 —— 否则访客会以为要注册才能改
    expect(screen.getByText(/不需要登录/)).toBeInTheDocument();
  });
});
