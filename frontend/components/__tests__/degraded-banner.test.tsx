import { act, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  DegradedBanner,
  describeDegradedMode,
} from "@/components/degraded-banner";
import {
  ApiError,
  NetworkError,
  getHealth,
  type ApiResult,
  type HealthData,
} from "@/lib/api";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    getHealth: vi.fn(),
    planTrip: vi.fn(),
  };
});

const getHealthMock = vi.mocked(getHealth);

type HealthResult = ApiResult<HealthData>;

const HEALTHY: HealthResult = {
  data: {
    status: "ok",
    version: "0.1.0",
    env: "local",
    schema_state: "ok",
    degraded_modes: [],
    providers: {},
    database: {
      ok: true,
      latency_ms: 3,
      active_places: 200,
      routes: 30,
      cities: 1,
      kb_version: "2024.09",
      schema_ready: true,
    },
    config: {
      scoring_version: "s1",
      limits_version: "l1",
      ttl_version: "t1",
      pricing_version: "p1",
      pricing_calibrated: false,
    },
  },
  meta: {
    request_id: "health-1",
    cached: false,
    cache_layer: null,
    degraded_modes: [],
    elapsed_ms: 3,
  },
};

function withDegradedModes(modes: string[]): HealthResult {
  return {
    ...HEALTHY,
    data: { ...HEALTHY.data, status: "degraded", degraded_modes: modes },
  };
}

describe("describeDegradedMode", () => {
  it("把机器码翻译成人话", () => {
    expect(describeDegradedMode("llm:missing")).toBe(
      "未配置 LLM：将使用规则引擎",
    );
    expect(describeDegradedMode("search")).toBe("未配置搜索：仅用本地知识库");
    expect(describeDegradedMode("map:estimated")).toBe(
      "地图：距离为路网或估算值",
    );
    expect(describeDegradedMode("something_new")).toBe(
      "降级模式：something_new",
    );
  });

  it("★ 原型链上的键不能穿过后端回退（否则渲染 function/object 会崩整页）", () => {
    // `DEGRADED_LABELS["constructor"]` 是 `Object` 构造函数、
    // `["__proto__"]` 是一个对象 —— 而 `??` 只拦 null/undefined，
    // 它们会被当成文案交给 React 渲染，直接抬错（而且没有 error boundary 兜底）。
    for (const key of ["constructor", "__proto__", "toString", "valueOf"]) {
      const label = describeDegradedMode(`${key}:whatever`);
      expect(typeof label).toBe("string");
      expect(label).toBe(`降级模式：${key}:whatever`);
    }
  });
});

describe("DegradedBanner", () => {
  beforeEach(() => {
    getHealthMock.mockReset();
  });

  it("后端可用时显示连接状态、知识库规模与版本", async () => {
    getHealthMock.mockResolvedValue(HEALTHY);
    render(<DegradedBanner />);

    expect(await screen.findByText(/后端已连接/)).toBeInTheDocument();
    expect(screen.getByText(/知识库 200 个地点 \/ 30 条路线/)).toBeInTheDocument();
    expect(screen.getByText(/版本 0.1.0/)).toBeInTheDocument();
    expect(screen.queryByText(/未连接到后端服务/)).toBeNull();
  });

  it("有降级模式时用浅色提示条逐条列出", async () => {
    getHealthMock.mockResolvedValue(
      withDegradedModes(["llm:missing", "search:seed_only", "map:estimated"]),
    );
    render(<DegradedBanner />);

    expect(await screen.findByText("当前降级模式")).toBeInTheDocument();
    expect(
      screen.getByText("未配置 LLM：将使用规则引擎"),
    ).toBeInTheDocument();
    expect(screen.getByText("未配置搜索：仅用本地知识库")).toBeInTheDocument();
    expect(screen.getByText("地图：距离为路网或估算值")).toBeInTheDocument();
  });

  it("★ 后端返回原型链上的键名时仍能渲染，不崩整页", async () => {
    getHealthMock.mockResolvedValue(
      withDegradedModes(["__proto__:x", "constructor:y"]),
    );
    render(<DegradedBanner />);

    expect(await screen.findByText("当前降级模式")).toBeInTheDocument();
    expect(screen.getByText("降级模式：__proto__:x")).toBeInTheDocument();
    expect(screen.getByText("降级模式：constructor:y")).toBeInTheDocument();
  });

  it("后端不可用时显示「未连接到后端服务」，不抛错", async () => {
    getHealthMock.mockRejectedValue(new NetworkError(new Error("ECONNREFUSED")));
    render(<DegradedBanner />);

    expect(await screen.findByText("未连接到后端服务")).toBeInTheDocument();
    expect(screen.getByText(/make dev-backend/)).toBeInTheDocument();
  });

  it("健康检查返回 ApiError 时也只显示状态，不崩", async () => {
    getHealthMock.mockRejectedValue(
      new ApiError(
        { code: "INTERNAL", message: "boom", hint: "", context: {} },
        500,
        null,
      ),
    );

    render(<DegradedBanner />);

    expect(await screen.findByText(/后端状态检查失败/)).toBeInTheDocument();
    expect(screen.getByText(/HTTP 500/)).toBeInTheDocument();
    expect(screen.getByText(/INTERNAL/)).toBeInTheDocument();
  });

  it("卸载后才返回的健康检查不会报错，也不会写回已卸载的组件", async () => {
    let resolveHealth: (value: HealthResult) => void = () => undefined;
    getHealthMock.mockImplementation(
      () =>
        new Promise<HealthResult>((resolve) => {
          resolveHealth = resolve;
        }),
    );

    const { unmount } = render(<DegradedBanner />);
    expect(screen.getByText(/正在检查后端状态/)).toBeInTheDocument();

    unmount();

    await act(async () => {
      resolveHealth(HEALTHY);
    });

    expect(screen.queryByText(/后端已连接/)).toBeNull();
  });
});
