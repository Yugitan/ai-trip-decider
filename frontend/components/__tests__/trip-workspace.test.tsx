/**
 * 结果面板的三个操作：**改路线 / 撤销 / 分享**（`components/trip-workspace.tsx`）。
 *
 * 这一块的风险不在"能不能点"，而在**改完之后显示的是不是真的那一版**：
 * 后端的修改是"生成新版本"（换一条 trip_id），界面必须跟着切过去并重新取回。
 * 如果只在本地拼一拼旧对象，用户就会看到一份既不是旧的、也不是新的行程 ——
 * 而且刷新一下就露馅。所以下面每一条成功的用例都同时钉住两件事：
 *   ① 调用了正确的接口与参数；② 之后 `getTrip` 用的是**新的** trip_id。
 *
 * 另外三条诚实性要求：
 * - 「没听懂」是反问（`needs_clarification`），不是错误，行程保持不动；
 * - 「已经是最早的版本了」是边界，界面照原话说，不翻译成"操作失败"；
 * - 取消分享会让旧链接**立刻失效**，文案必须写清楚。
 */

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  ApiError,
  NetworkError,
  getTrip,
  reviseTrip,
  shareTrip,
  undoTrip,
  type TripOut,
} from "@/lib/api";
import { TripWorkspace } from "@/components/trip-workspace";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    getTrip: vi.fn(),
    reviseTrip: vi.fn(),
    undoTrip: vi.fn(),
    shareTrip: vi.fn(),
  };
});

const getTripMock = vi.mocked(getTrip);
const reviseMock = vi.mocked(reviseTrip);
const undoMock = vi.mocked(undoTrip);
const shareMock = vi.mocked(shareTrip);

const META = {
  request_id: "req-1",
  cached: false,
  cache_layer: null,
  degraded_modes: [],
  elapsed_ms: 12,
};

function makeTrip(overrides: Partial<TripOut> = {}): TripOut {
  return {
    trip_id: "trip-1",
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
    routes: [
      {
        id: "route-1",
        label: "A",
        archetype: "relaxed",
        theme: null,
        name: "老城寻味慢行",
        one_liner: "3 站 · 约 5.3 小时 · 步行 0.5 km",
        place_count: 3,
        total_duration_min: 317,
        walking_distance_m: 537,
        transit_time_min: 17,
        transit_distance_m: 2969,
        budget_min: "18.32",
        budget_max: "18.32",
        budget_scope: "per_person",
        budget_estimated: true,
        budget_unknown_items: [],
        recommend_score: 0.97,
        feasibility: { feasible: true, violations: [], warnings: [], metrics: {} },
        best_for: [],
        highlights: [],
        pros: [],
        cons: [],
        recommendation_reason: null,
        route_source: "generated",
        template_route_id: null,
        stops: [],
      },
    ],
    ...overrides,
  };
}

/** 第 2 版：名字不同，用来断言"界面真的换成了新版本"。 */
const REVISED_TRIP = makeTrip({
  trip_id: "trip-2",
  revision_no: 2,
  routes: [
    {
      ...makeTrip().routes[0]!,
      id: "route-2",
      name: "园林茶点线",
    },
  ],
});

beforeEach(() => {
  getTripMock.mockReset();
  reviseMock.mockReset();
  undoMock.mockReset();
  shareMock.mockReset();

  // 按 id 返回：只有切到了 trip-2 才可能看到新路线名
  getTripMock.mockImplementation((id: string) =>
    Promise.resolve({ data: id === "trip-2" ? REVISED_TRIP : makeTrip(), meta: META }),
  );
  shareMock.mockResolvedValue({
    data: { slug: "abc123456789", url: "http://localhost:3000/t/abc123456789", is_public: true },
    meta: META,
  });
});

/** 等行程渲染出来（后面才有输入框与按钮可点）。 */
async function renderWorkspace() {
  render(<TripWorkspace tripId="trip-1" />);
  await screen.findByTestId("trip-result");
}

async function typeInstruction(text: string) {
  const user = userEvent.setup();
  await user.type(screen.getByLabelText(/想改哪儿/), text);
  return user;
}

/**
 * 装上自己的剪贴板替身。
 *
 * ⚠️ 必须在 `userEvent.setup()` **之后**调用：user-event 自己会往 navigator 上装一个
 * 剪贴板实现，先装就会被它盖掉 —— 那样点击确实成功，但断言的是另一个对象，
 * 表现为"复制没生效"。
 */
function stubClipboard(writeText: (text: string) => Promise<void>) {
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: { writeText },
  });
}

// ── 取回行程 ────────────────────────────────────────────────────────────────

describe("取回行程", () => {
  it("加载中给出可见反馈", () => {
    getTripMock.mockReturnValue(new Promise(() => {}));
    render(<TripWorkspace tripId="trip-1" />);

    expect(screen.getByTestId("trip-loading")).toBeInTheDocument();
    expect(screen.getByText(/正在取回完整行程/)).toBeInTheDocument();
    expect(getTripMock).toHaveBeenCalledWith("trip-1");
  });

  it("★ 403 时如实转述后端说法（而不是报「后端未启动」）", async () => {
    getTripMock.mockRejectedValue(
      new ApiError(
        {
          code: "FORBIDDEN",
          message: "这个行程不属于当前会话",
          hint: "请回到创建它的浏览器（游客行程按浏览器会话隔离）。",
          context: {},
        },
        403,
        "req-403",
      ),
    );
    render(<TripWorkspace tripId="trip-1" />);

    expect(await screen.findByTestId("trip-error")).toBeInTheDocument();
    expect(screen.getByText("没能取回这次行程")).toBeInTheDocument();
    expect(screen.getByText("这个行程不属于当前会话")).toBeInTheDocument();
    expect(screen.getByText(/请回到创建它的浏览器/)).toBeInTheDocument();
    expect(screen.getByText(/HTTP 403/)).toBeInTheDocument();
    expect(screen.getByText(/错误码 FORBIDDEN/)).toBeInTheDocument();
    expect(screen.getByText(/request_id req-403/)).toBeInTheDocument();
  });

  it("网络中断时提示后端未启动，并可重试成功", async () => {
    getTripMock.mockRejectedValueOnce(new NetworkError(new Error("ECONNREFUSED")));
    getTripMock.mockResolvedValueOnce({ data: makeTrip(), meta: META });

    const user = userEvent.setup();
    render(<TripWorkspace tripId="trip-1" />);

    expect(await screen.findByText("没能连上后端服务。")).toBeInTheDocument();
    expect(screen.getByText(/make dev-backend/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "重试" }));

    expect(await screen.findByTestId("trip-result")).toBeInTheDocument();
    expect(getTripMock).toHaveBeenCalledTimes(2);
    expect(screen.queryByTestId("trip-error")).toBeNull();
  });

  it("未知异常也给出可见反馈", async () => {
    getTripMock.mockRejectedValue(new Error("boom"));
    render(<TripWorkspace tripId="trip-1" />);

    expect(await screen.findByText("发生了未预期的错误。")).toBeInTheDocument();
  });
});

// ── 改路线 ──────────────────────────────────────────────────────────────────

describe("改路线", () => {
  it("提交指令 → 调 revise → 切换到新版本并重新取回", async () => {
    reviseMock.mockResolvedValue({
      data: {
        trip_id: "trip-2",
        revision_no: 2,
        diff: { sentence: "天数 1 天 → 2 天", days_before: 1, days_after: 2 },
        needs_clarification: null,
      },
      meta: META,
    });

    await renderWorkspace();
    const user = await typeInstruction("改成 2 天");
    await user.click(screen.getByRole("button", { name: "改路线" }));

    expect(reviseMock).toHaveBeenCalledWith("trip-1", "改成 2 天");
    // 后端说的话原样显示，不自己拼一句
    expect(await screen.findByTestId("revision-note")).toHaveTextContent("天数 1 天 → 2 天");
    expect(screen.getByTestId("revision-note")).toHaveTextContent("已生成第 2 版");
    // ★ 关键：切到新 trip_id 并把内容重新取回来
    expect(getTripMock).toHaveBeenLastCalledWith("trip-2");
    expect(await screen.findByText("园林茶点线")).toBeInTheDocument();
    expect(screen.queryByText("老城寻味慢行")).toBeNull();
    // 指令框清空，便于继续说下一句
    expect(screen.getByLabelText(/想改哪儿/)).toHaveValue("");
  });

  it("★ 没听懂时当成反问，行程保持不变", async () => {
    reviseMock.mockResolvedValue({
      data: {
        trip_id: "trip-1",
        revision_no: 1,
        diff: {},
        needs_clarification: "没能理解「随便改改」。",
      },
      meta: META,
    });

    await renderWorkspace();
    const user = await typeInstruction("随便改改");
    await user.click(screen.getByRole("button", { name: "改路线" }));

    const question = await screen.findByTestId("revision-clarify");
    expect(question).toHaveTextContent("没能理解「随便改改」。");
    // 不报错、不换版本、内容还是那一份
    expect(screen.queryByTestId("revision-error")).toBeNull();
    expect(screen.queryByTestId("revision-note")).toBeNull();
    expect(getTripMock).toHaveBeenCalledTimes(1);
    expect(screen.getByText("老城寻味慢行")).toBeInTheDocument();
  });

  it("空指令不能提交（按钮禁用）", async () => {
    await renderWorkspace();

    expect(screen.getByRole("button", { name: "改路线" })).toBeDisabled();
    expect(reviseMock).not.toHaveBeenCalled();
  });

  it("失败时展示后端的 code/hint，不假装成功", async () => {
    reviseMock.mockRejectedValue(
      new ApiError(
        {
          code: "NO_FEASIBLE_ROUTE",
          message: "在你给出的限制下排不出可行的路线",
          hint: "试着放宽预算、步行量或减少偏好/排除项。",
          context: {},
        },
        200,
        "req-9",
      ),
    );

    await renderWorkspace();
    const user = await typeInstruction("全程只能走路");
    await user.click(screen.getByRole("button", { name: "改路线" }));

    const block = await screen.findByTestId("revision-error");
    expect(block).toHaveTextContent("没能改成你要的样子");
    expect(block).toHaveTextContent("排不出可行的路线");
    expect(block).toHaveTextContent("错误码 NO_FEASIBLE_ROUTE");
    expect(block).toHaveTextContent(/试着放宽预算/);
    // 行程没有被换掉
    expect(getTripMock).toHaveBeenCalledTimes(1);
  });
});

// ── 撤销 ────────────────────────────────────────────────────────────────────

describe("撤销", () => {
  it("撤销成功 → 显示切回哪一版并重新取回", async () => {
    undoMock.mockResolvedValue({ data: makeTrip({ trip_id: "trip-0" }), meta: META });

    await renderWorkspace();
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "撤销这次修改" }));

    expect(undoMock).toHaveBeenCalledWith("trip-1");
    expect(getTripMock).toHaveBeenLastCalledWith("trip-0");
    expect(await screen.findByTestId("undo-note")).toHaveTextContent("已回到第 1 版");
    expect(screen.queryByTestId("undo-error")).toBeNull();
  });

  it("★ 已经是最早的版本时照原话说（这是边界，不是故障）", async () => {
    undoMock.mockRejectedValue(
      new ApiError(
        {
          code: "INVALID_INPUT",
          message: "这已经是最早的版本了，没有可以撤销的修改。",
          hint: "继续修改会生成新的版本。",
          context: {},
        },
        422,
        null,
      ),
    );

    await renderWorkspace();
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "撤销这次修改" }));

    const block = await screen.findByTestId("undo-error");
    expect(block).toHaveTextContent("这已经是最早的版本了");
    expect(block).toHaveTextContent(/继续修改会生成新的版本/);
    expect(block).toHaveTextContent("错误码 INVALID_INPUT");
    // 内容没有变
    expect(getTripMock).toHaveBeenCalledTimes(1);
    expect(screen.getByText("老城寻味慢行")).toBeInTheDocument();
  });
});

// ── 分享 ────────────────────────────────────────────────────────────────────

describe("分享", () => {
  it("生成公开链接 → 显示接口给的地址并可复制", async () => {
    const user = userEvent.setup();
    const writeText = vi.fn().mockResolvedValue(undefined);
    stubClipboard(writeText);

    await renderWorkspace();
    await user.click(screen.getByRole("button", { name: "生成公开链接" }));

    expect(shareMock).toHaveBeenCalledWith("trip-1", true);
    const link = await screen.findByRole("link", { name: /localhost:3000\/t\// });
    expect(link).toHaveAttribute("href", "http://localhost:3000/t/abc123456789");
    expect(screen.getByTestId("share-note")).toHaveTextContent("任何拿到它的人都能看到");

    await user.click(screen.getByRole("button", { name: "复制链接" }));
    expect(writeText).toHaveBeenCalledWith("http://localhost:3000/t/abc123456789");
    expect(await screen.findByRole("button", { name: "已复制" })).toBeInTheDocument();
  });

  it("★ 取消分享要说清链接立刻失效（后端真的会 404）", async () => {
    shareMock.mockResolvedValueOnce({
      data: { slug: "abc123456789", url: "http://localhost:3000/t/abc123456789", is_public: true },
      meta: META,
    });
    shareMock.mockResolvedValueOnce({
      data: { slug: "abc123456789", url: "http://localhost:3000/t/abc123456789", is_public: false },
      meta: META,
    });

    await renderWorkspace();
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "生成公开链接" }));
    await screen.findByRole("link", { name: /t\/abc123456789/ });

    await user.click(screen.getByRole("button", { name: "取消分享" }));

    expect(shareMock).toHaveBeenLastCalledWith("trip-1", false);
    expect(screen.queryByRole("link", { name: /t\/abc123456789/ })).toBeNull();
    expect(await screen.findByTestId("share-note")).toHaveTextContent("打不开");
  });

  it("剪贴板不可用时不假装复制成功", async () => {
    const user = userEvent.setup();
    stubClipboard(vi.fn().mockRejectedValue(new Error("denied")));

    await renderWorkspace();
    await user.click(screen.getByRole("button", { name: "生成公开链接" }));
    await screen.findByRole("link", { name: /t\// });
    await user.click(screen.getByRole("button", { name: "复制链接" }));

    expect(await screen.findByTestId("share-note")).toHaveTextContent("请手动复制");
  });

  it("失败时展示原因，且不显示任何链接", async () => {
    shareMock.mockRejectedValue(
      new ApiError(
        { code: "INTERNAL", message: "生成分享链接失败，请重试", hint: "", context: {} },
        500,
        null,
      ),
    );

    await renderWorkspace();
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "生成公开链接" }));

    const block = await screen.findByTestId("share-error");
    expect(block).toHaveTextContent("没能生成分享链接");
    expect(block).toHaveTextContent("生成分享链接失败，请重试");
    expect(screen.queryByRole("link", { name: /t\// })).toBeNull();
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "生成公开链接" })).toBeEnabled(),
    );
  });
});
