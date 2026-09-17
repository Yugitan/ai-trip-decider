/**
 * 成本后台的测试（PRD FR-12 AC-12.2）。
 *
 * ★ 最重要的一条：未校准的窗口里不许报\"达标\" ★
 * 后端在这种情况下回 `within_target: null`，界面必须说\"无法判断\"。
 * 一个绿色的 ✅ 配上\"单价未校准\"的注脚，等于把两个互相矛盾的说法摆在一起。
 */

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { AdminCostDashboard } from "@/components/admin-cost-dashboard";
import { ApiError, type CostSummary } from "@/lib/api";

const getCostSummaryMock = vi.fn<
  (days: number, token: string | null) => Promise<{ data: CostSummary; meta: never }>
>();

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, getCostSummary: (days: number, token: string | null) => getCostSummaryMock(days, token) };
});

function summary(overrides: Partial<CostSummary> = {}): CostSummary {
  return {
    window_days: 7,
    by_category: [{ category: "llm", calls: 11, amount_cny: "0.4200", cache_hits: 3 }],
    by_provider: [
      { provider: "deepseek", category: "llm", calls: 11, amount_cny: "0.4200", cache_hits: 3 },
    ],
    plans: {
      plans: 4,
      avg_cny: 0.105,
      p50_cny: 0.09,
      p95_cny: 0.3,
      max_cny: 0.3,
      uncalibrated_plans: 0,
      rows_without_request: 2,
      target_cny: 0.5,
      within_target: true,
      top: [
        {
          request_id: "11111111-1111-1111-1111-111111111111",
          amount_cny: "0.3000",
          calls: 3,
          uncalibrated: false,
        },
      ],
    },
    cache: { calls: 11, hits: 3, hit_rate: 0.2727 },
    pricing_calibrated: true,
    uncalibrated_rows: 0,
    note: "全部单价已校准",
    daily: { spent_cny: "0.4200", limit_cny: 20, exceeded: false },
    breakers: {
      plan_total_cny: 1,
      plan_search_cny: 0.3,
      plan_map_calls: 40,
      plan_llm_calls: 4,
    },
    ...overrides,
  };
}

/**
 * 内存版 sessionStorage。
 *
 * 这个测试进程里**没有全局 sessionStorage**（Node 22 不带它），
 * 而成本页通过 `lib/dev-api.ts` 的存储函数读写 Token —— 那函数在取不到存储时
 * 返回 `null`（隐私模式也走同一条路）。要断言"填过的 Token 会被记住"，
 * 就得自己装一份，而不是假设环境里有。
 */
function memoryStorage(): Storage {
  const map = new Map<string, string>();
  return {
    get length() {
      return map.size;
    },
    clear: () => map.clear(),
    getItem: (key: string) => map.get(key) ?? null,
    key: (index: number) => [...map.keys()][index] ?? null,
    removeItem: (key: string) => void map.delete(key),
    setItem: (key: string, value: string) => void map.set(key, value),
  } as Storage;
}

beforeEach(() => {
  getCostSummaryMock.mockReset();
  vi.stubGlobal("sessionStorage", memoryStorage());
});

describe("AdminCostDashboard", () => {
  it("首屏按近 7 天读取，并把单次规划的关键数字摆出来", async () => {
    getCostSummaryMock.mockResolvedValue({ data: summary(), meta: undefined as never });
    render(<AdminCostDashboard />);

    await waitFor(() => expect(getCostSummaryMock).toHaveBeenCalledWith(7, null));
    expect(await screen.findByText("¥0.1050")).toBeInTheDocument(); // 平均
    expect(screen.getByText("¥0.0900")).toBeInTheDocument(); // P50
    // P95 与"最贵的那一笔"会是同一个数（同一份数据的两处呈现），所以允许出现多次
    expect(screen.getAllByText("¥0.3000").length).toBeGreaterThan(0);
    // 单次规划是\"按 request_id 聚合\"的：所以显示笔数而不是调用次数
    expect(screen.getByText("规划笔数")).toBeInTheDocument();
    expect(screen.getByText("27.3%")).toBeInTheDocument(); // 缓存命中率
    // 没有 request_id 的记录不计入平均 —— 必须说出来，否则读者会以为它被算进去了
    expect(screen.getByText(/另有 2 条调用没有 request_id/)).toBeInTheDocument();
    expect(screen.getByTestId("admin-top-plans").textContent).toContain(
      "11111111-1111-1111-1111-111111111111",
    );
  });

  it("★ 有未校准记录时说\"无法判断\"，不报达标 ★", async () => {
    getCostSummaryMock.mockResolvedValue({
      data: summary({
        pricing_calibrated: false,
        uncalibrated_rows: 5,
        note: "存在未校准记录（写入时单价为 null，或早于价目表校准），金额不代表真实支出",
        plans: { ...summary().plans, within_target: null, uncalibrated_plans: 1 },
      }),
      meta: undefined as never,
    });
    render(<AdminCostDashboard />);

    expect(await screen.findByText(/无法判断（窗口内有未校准记录）/)).toBeInTheDocument();
    expect(screen.queryByText(/^ 达标$/)).not.toBeInTheDocument();
    expect(screen.getByTestId("admin-pricing-note").textContent).toContain("不代表真实支出");
    expect(screen.getByTestId("admin-pricing-note").textContent).toContain("未校准记录 5 条");
  });

  it("超标时明确说超标（而不是留一个中性色数字）", async () => {
    getCostSummaryMock.mockResolvedValue({
      data: summary({ plans: { ...summary().plans, within_target: false, p95_cny: 0.9 } }),
      meta: undefined as never,
    });
    render(<AdminCostDashboard />);
    expect(await screen.findByText(/超标/)).toBeInTheDocument();
  });

  it("401 时显示后端原话与提示，并把错误码摆出来", async () => {
    getCostSummaryMock.mockRejectedValue(
      new ApiError(
        { code: "ADMIN_REQUIRED", message: "后台令牌不正确", hint: "填入 ADMIN_TOKEN。", context: {} },
        401,
        null,
      ),
    );
    render(<AdminCostDashboard />);

    const panel = await screen.findByTestId("admin-error");
    expect(panel.textContent).toContain("后台令牌不正确");
    expect(panel.textContent).toContain("填入 ADMIN_TOKEN。");
    expect(panel.textContent).toContain("ADMIN_REQUIRED");
    expect(screen.queryByTestId("admin-top-plans")).not.toBeInTheDocument();
  });

  it("连不上后端时说\"连不上\"，不说\"令牌错了\"", async () => {
    getCostSummaryMock.mockRejectedValue(new Error("boom"));
    render(<AdminCostDashboard />);
    const panel = await screen.findByTestId("admin-error");
    expect(panel.textContent).toContain("无法连接到后端服务");
    expect(panel.textContent).toContain("NETWORK");
  });

  it("填了 Token 就带上它，并记在本地（不记 cookie）", async () => {
    getCostSummaryMock.mockResolvedValue({ data: summary(), meta: undefined as never });
    render(<AdminCostDashboard />);
    await waitFor(() => expect(getCostSummaryMock).toHaveBeenCalledTimes(1));

    await userEvent.type(screen.getByTestId("admin-token"), "s3cret");
    await userEvent.click(screen.getByTestId("admin-load"));

    await waitFor(() => expect(getCostSummaryMock).toHaveBeenCalledWith(7, "s3cret"));
    // 与 /dev 面板同一个键：在面板里填过就不必在成本页再填一次
    expect(sessionStorage.getItem("tripdecider.dev.admin_token")).toBe("s3cret");
  });

  it("换统计窗口会重新读，并且只读新的窗口", async () => {
    getCostSummaryMock.mockResolvedValue({ data: summary(), meta: undefined as never });
    render(<AdminCostDashboard />);
    await waitFor(() => expect(getCostSummaryMock).toHaveBeenCalledTimes(1));

    await userEvent.selectOptions(screen.getByTestId("admin-days"), "30");
    await waitFor(() => expect(getCostSummaryMock).toHaveBeenLastCalledWith(30, null));
  });

  it("窗口内没有记录时明说没有，而不是渲染一张空表", async () => {
    getCostSummaryMock.mockResolvedValue({
      data: summary({
        by_category: [],
        plans: { ...summary().plans, plans: 0, top: [], avg_cny: null, p50_cny: null, p95_cny: null },
      }),
      meta: undefined as never,
    });
    render(<AdminCostDashboard />);
    expect(await screen.findByText(/窗口内没有任何调用记录/)).toBeInTheDocument();
    expect(screen.getByText(/还没有带 request_id 的规划/)).toBeInTheDocument();
    // 空窗口的命中率与分位是"未知"而不是 0（0 会读成"从来没命中过"）
    expect(screen.getAllByText("未知").length).toBeGreaterThan(0);
  });
});
