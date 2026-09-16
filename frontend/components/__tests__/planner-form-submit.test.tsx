import { existsSync, readFileSync } from "node:fs";
import path from "node:path";

import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  ApiError,
  NetworkError,
  getTrip,
  planTrip,
  type ApiResult,
  type TripOut,
} from "@/lib/api";
import { PREFERENCE_OPTIONS, PlannerForm } from "@/components/planner-form";

/**
 * 读后端的 `config/scoring.yaml`，把 `preference_dimensions` 的键抠出来。
 *
 * 为什么要跨语言读一个 YAML：偏好选项是**前端硬编码**的列表，而合法取值由后端定义（PRD 口径）。
 * 两边一旦不同步（新增一个偏好忘了改另一头），用户看到的就是一个 422，
 * 而两边的单测、lint、类型检查**全都是绿的**。这条断言让漏配变成一条会红的测试。
 */
function backendPreferenceKeys(): string[] {
  // 两种跑法都要能找到：`pnpm test`（cwd=frontend）与在仓库根目录调 vitest
  const candidates = [
    path.join(process.cwd(), "config", "scoring.yaml"),
    path.join(process.cwd(), "..", "config", "scoring.yaml"),
  ];
  const found = candidates.find((candidate) => existsSync(candidate));
  // 找不到就报错，而不是静静跳过 —— 跳过的“契约测试”等于没有
  if (found === undefined) {
    throw new Error(`找不到 config/scoring.yaml，已尝试：${candidates.join(" / ")}`);
  }
  const lines = readFileSync(found, "utf8").split("\n");
  const start = lines.findIndex((line) => line.startsWith("preference_dimensions:"));
  expect(start).toBeGreaterThanOrEqual(0);
  const keys: string[] = [];
  for (const line of lines.slice(start + 1)) {
    if (/^\S/.test(line)) break; // 回到顶层，说明这个块结束了
    const match = /^ {2}([a-z][a-z0-9_]*):/.exec(line);
    const key = match?.[1];
    if (key !== undefined) keys.push(key);
  }
  return keys.sort();
}

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    planTrip: vi.fn(),
    getHealth: vi.fn(),
    // 受理之后表单会去 GET /trips/{id} 取回行程（见 trip-result.tsx）。
    // 这里必须一起换掉：否则单元测试会真的发一个网络请求出去。
    getTrip: vi.fn(),
  };
});

const planTripMock = vi.mocked(planTrip);
const getTripMock = vi.mocked(getTrip);

/** 受理之后的那次取回：给一份最小行程即可，本文件关心的是提交这一侧。 */
const TRIP: TripOut = {
  trip_id: "trip-1",
  request_id: "req-1",
  city: "guangzhou",
  title: "广州 · 09:00–21:00",
  days: 1,
  revision_no: 1,
  route_count: 3,
  route_count_requested: 3,
  intent: {},
  degraded_modes: [],
  total_cost_cny: "0",
  generation_ms: 12,
  created_at: null,
  is_public: false,
  share_slug: null,
  routes: [],
};

beforeEach(() => {
  getTripMock.mockReset();
  getTripMock.mockResolvedValue({
    data: TRIP,
    meta: {
      request_id: "req-1",
      cached: false,
      cache_layer: null,
      degraded_modes: [],
      elapsed_ms: 12,
    },
  });
});

type PlanResult = ApiResult<{ request_id: string; stream_url: string }>;

const SUCCESS: PlanResult = {
  data: { request_id: "req-1", stream_url: "/api/v1/trips/req-1/stream" },
  meta: {
    request_id: "req-1",
    cached: false,
    cache_layer: null,
    degraded_modes: [],
    elapsed_ms: 12,
  },
};

// ⚠️ 这里必须是**接口取值**（slug / 枚举 key），不是界面上的中文
const DEFAULT_PAYLOAD = {
  city: "guangzhou",
  days: 1,
  // 默认的游玩时长：一天（半天/尽可能多是用户可以显式选的另外两档）
  day_span: "full_day",
  people: 2,
  preferences: [],
  pace: "relaxed",
  budget: { amount: 300, scope: "per_person" },
  free_text: "",
};

describe("PlannerForm 提交", () => {
  beforeEach(() => {
    planTripMock.mockReset();
  });

  it("提交时调用 planTrip，payload 结构与 PlanRequest 一致", async () => {
    planTripMock.mockResolvedValue(SUCCESS);
    const user = userEvent.setup();
    render(<PlannerForm />);

    await user.click(screen.getByRole("button", { name: "开始规划" }));

    expect(planTripMock).toHaveBeenCalledTimes(1);
    expect(planTripMock).toHaveBeenCalledWith(DEFAULT_PAYLOAD);
  });

  it("用户改过的字段会体现在 payload 里", async () => {
    planTripMock.mockRejectedValue(
      new ApiError(
        { code: "NOT_FOUND", message: "Not Found", hint: "", context: {} },
        404,
        null,
      ),
    );
    const user = userEvent.setup();
    render(<PlannerForm />);

    await user.click(screen.getByRole("radio", { name: "3 天" }));
    // 「玩几天」与「每天玩多久」是两个独立字段：只改前者的话，本地人想玩半天
    // 就只能自己去改结束时间 —— 所以这两个控件各要有一个改过的值进 payload。
    await user.click(screen.getByRole("radio", { name: "半天" }));
    await user.click(screen.getByRole("radio", { name: "紧凑" }));
    await user.click(screen.getByRole("checkbox", { name: /夜景/ }));
    await user.click(screen.getByRole("checkbox", { name: /美食/ }));
    await user.click(screen.getByRole("radio", { name: "总计" }));
    await user.click(screen.getByRole("button", { name: "开始规划" }));

    expect(planTripMock).toHaveBeenCalledWith({
      city: "guangzhou",
      days: 3,
      day_span: "half_day",
      people: 2,
      preferences: ["food", "night_view"],
      pace: "packed",
      budget: { amount: 300, scope: "total" },
      free_text: "",
    });
  });

  it("★ 提交的是接口枚举值，不是界面上的中文标签（曾经稳定 422）", async () => {
    planTripMock.mockResolvedValue(SUCCESS);
    const user = userEvent.setup();
    render(<PlannerForm />);

    await user.click(screen.getByRole("checkbox", { name: /美食/ }));
    await user.click(screen.getByRole("button", { name: "开始规划" }));

    const payload = planTripMock.mock.calls[0]?.[0];
    expect(payload?.city).toBe("guangzhou");
    expect(payload?.preferences).toEqual(["food"]);
    // 界面上仍然是中文（映射只发生在 payload 这一层，用户看到的东西没变）
    expect(screen.getByRole("checkbox", { name: /美食/ })).toBeChecked();
  });

  it("★ 偏好选项必须与后端 preference_dimensions 一一对应", () => {
    expect(PREFERENCE_OPTIONS.map((option) => option.api).sort()).toEqual(
      backendPreferenceKeys(),
    );
    // 每个选项都得有接口取值，不能让某个漏配的选项悄悄发中文出去
    for (const option of PREFERENCE_OPTIONS) {
      expect(option.api, `${option.key} 缺 api 枚举值`).toMatch(/^[a-z][a-z0-9_]*$/);
    }
  });

  it("提交期间按钮禁用 + aria-busy + 可见反馈，且无法重复提交", async () => {
    let resolvePlan: (value: PlanResult) => void = () => undefined;
    planTripMock.mockImplementation(
      () =>
        new Promise<PlanResult>((resolve) => {
          resolvePlan = resolve;
        }),
    );

    const user = userEvent.setup();
    render(<PlannerForm />);

    await user.click(screen.getByRole("button", { name: "开始规划" }));

    const pendingButton = screen.getByRole("button", { name: /正在提交…/ });
    expect(pendingButton).toBeDisabled();
    expect(pendingButton).toHaveAttribute("aria-busy", "true");
    expect(
      screen.getByText("正在把这份需求发给规划服务…"),
    ).toBeInTheDocument();
    // 按钮在任何状态下都有可见反馈：文字从「开始规划」变成「正在提交…」
    expect(screen.queryByRole("button", { name: "开始规划" })).toBeNull();

    await user.click(pendingButton);
    expect(planTripMock).toHaveBeenCalledTimes(1);

    await act(async () => {
      resolvePlan(SUCCESS);
    });

    expect(
      await screen.findByText("后端已接受这次规划请求"),
    ).toBeInTheDocument();
    expect(screen.getByText(/request_id：req-1/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "开始规划" })).toBeEnabled();
  });
});

describe("PlannerForm 后端未实现时的处理", () => {
  beforeEach(() => {
    planTripMock.mockReset();
  });

  it("ApiError(404)：指向地址/服务，而不是推给一个已经完成的里程碑，并展示真正发出去的 payload", async () => {
    planTripMock.mockRejectedValue(
      new ApiError(
        {
          code: "NOT_FOUND",
          message: "Not Found",
          hint: "没有这个路由。",
          context: {},
        },
        404,
        "req-abc",
      ),
    );

    const user = userEvent.setup();
    render(<PlannerForm />);

    await user.click(screen.getByRole("button", { name: "开始规划" }));

    expect(await screen.findByText(/后端没有这个接口（HTTP 404）/)).toBeInTheDocument();
    expect(screen.getByText(/后端没启动|版本过旧|地址不对/)).toBeInTheDocument();
    expect(screen.getByText(/错误码 NOT_FOUND/)).toBeInTheDocument();
    expect(screen.getByText(/request_id req-abc/)).toBeInTheDocument();
    expect(screen.getByText(/没有这个路由。/)).toBeInTheDocument();

    const payloadBlock = screen.getByTestId("plan-payload");
    expect(JSON.parse(payloadBlock.textContent ?? "")).toEqual(DEFAULT_PAYLOAD);

    // 绝不出现编造的路线数据
    expect(screen.queryByText(/第 ?1 ?天/)).toBeNull();
    expect(screen.queryByText(/路线详情/)).toBeNull();
    expect(screen.queryByText("后端已接受这次规划请求")).toBeNull();

    // 按钮必须回到可见的可用状态，而不是卡在 loading
    expect(screen.getByRole("button", { name: "开始规划" })).toBeEnabled();
  });

  it("NetworkError：提示后端未启动，并保留请求内容", async () => {
    planTripMock.mockRejectedValue(new NetworkError(new Error("ECONNREFUSED")));

    const user = userEvent.setup();
    render(<PlannerForm />);

    await user.click(screen.getByRole("button", { name: "开始规划" }));

    expect(await screen.findByText("未连接到后端服务")).toBeInTheDocument();
    expect(screen.getByText(/make dev-backend/)).toBeInTheDocument();
    expect(screen.getByTestId("plan-payload")).toBeInTheDocument();
    expect(screen.queryByText("后端已接受这次规划请求")).toBeNull();
  });

  it("未知异常也会给出可见反馈，不会静默失败", async () => {
    planTripMock.mockRejectedValue(new Error("boom"));

    const user = userEvent.setup();
    render(<PlannerForm />);

    await user.click(screen.getByRole("button", { name: "开始规划" }));

    expect(await screen.findByText("规划请求失败")).toBeInTheDocument();
    expect(screen.getByTestId("plan-payload")).toBeInTheDocument();
  });
});

describe("PlannerForm 区分「接口没实现」与「输入被拒绝」", () => {
  beforeEach(() => {
    planTripMock.mockReset();
  });

  function rejectWith(status: number, code: string) {
    planTripMock.mockRejectedValue(
      new ApiError(
        { code, message: "后端拒绝了这次请求", hint: "", context: {} },
        status,
        null,
      ),
    );
  }

  it.each([404, 405, 501])(
    "HTTP %i：说是「后端没有这个接口」（M4 已完成，不该再叫用户等）",
    async (status) => {
      rejectWith(status, "NOT_FOUND");
      const user = userEvent.setup();
      render(<PlannerForm />);

      await user.click(screen.getByRole("button", { name: "开始规划" }));

      expect(
        await screen.findByText(new RegExp(`后端没有这个接口（HTTP ${status}）`)),
      ).toBeInTheDocument();
      // 回归：M4 已经交付，这条文案曾经写成「规划引擎正在开发中」
      expect(screen.queryByText(/正在开发中/)).not.toBeInTheDocument();
    },
  );

  it.each([400, 422])(
    "★ HTTP %i：必须说「需求不合法」，不能说成「接口不存在」",
    async (status) => {
      // 回归：早期实现把 400/422 也归进"引擎开发中"，并附上一句
      // "需求已被前端完整校验" —— 但 422 恰恰意味着后端认为这份需求不合法，
      // 那句话会把用户引向"等接口上线"而不是"改输入"。
      rejectWith(status, "INVALID_INPUT");
      const user = userEvent.setup();
      render(<PlannerForm />);

      await user.click(screen.getByRole("button", { name: "开始规划" }));

      expect(
        await screen.findByText(new RegExp(`后端认为这份需求不合法（HTTP ${status}）`)),
      ).toBeInTheDocument();
      expect(screen.queryByText(/后端没有这个接口/)).not.toBeInTheDocument();
    },
  );

  it("无法归类的状态码给出中性说明，不编造原因", async () => {
    rejectWith(503, "DB_UNAVAILABLE");
    const user = userEvent.setup();
    render(<PlannerForm />);

    await user.click(screen.getByRole("button", { name: "开始规划" }));

    expect(await screen.findByText(/这次请求没有被后端接受/)).toBeInTheDocument();
    expect(screen.queryByText(/后端没有这个接口/)).not.toBeInTheDocument();
  });
});
