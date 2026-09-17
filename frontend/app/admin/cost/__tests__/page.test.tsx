/**
 * `/admin/cost` 页面的测试（PRD FR-12 AC-12.2）。
 *
 * 两件事值得钉住：
 * 1. 这一页说的是**后端真正在生效的阈值**（由接口给），页面自己不写死数字；
 * 2. 未校准的金额必须被标注 —— 页面文案里就把这条规矩说出来，
 *    因为它是这个页面唯一的价值来源（数字能被相信）。
 */

import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import AdminCostPage from "@/app/admin/cost/page";
import type { CostSummary } from "@/lib/api";

const getCostSummaryMock = vi.fn();

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, getCostSummary: (...args: unknown[]) => getCostSummaryMock(...args) };
});

const SUMMARY: CostSummary = {
  window_days: 7,
  by_category: [],
  by_provider: [],
  plans: {
    plans: 0,
    avg_cny: null,
    p50_cny: null,
    p95_cny: null,
    max_cny: null,
    uncalibrated_plans: 0,
    rows_without_request: 0,
    target_cny: 0.5,
    within_target: null,
    top: [],
  },
  cache: { calls: 0, hits: 0, hit_rate: null },
  pricing_calibrated: true,
  uncalibrated_rows: 0,
  note: "全部单价已校准",
  daily: { spent_cny: "0", limit_cny: 20, exceeded: false },
  breakers: { plan_total_cny: 1, plan_search_cny: 0.3, plan_map_calls: 40, plan_llm_calls: 4 },
};

beforeEach(() => {
  getCostSummaryMock.mockReset();
});

describe("/admin/cost", () => {
  it("渲染标题、免责说明与\"同一份数据也能 make cost\"的出路", async () => {
    getCostSummaryMock.mockResolvedValue({ data: SUMMARY, meta: undefined as never });
    render(AdminCostPage());

    expect(screen.getByRole("heading", { name: "成本后台" })).toBeInTheDocument();
    expect(screen.getByText(/未校准的单价会被显式标注/)).toBeInTheDocument();
    expect(screen.getByText("make cost")).toBeInTheDocument();
    // 数据由客户端组件拉取；页面本身不预取（Token 只在浏览器里）
    await waitFor(() => expect(getCostSummaryMock).toHaveBeenCalled());
  });
});
