/**
 * 规划结果（`components/trip-result.tsx`）的测试。
 *
 * 这一块是「点开始规划之后到底看得到什么」的**唯一**出口，所以四类状态都要钉住：
 * 加载中 / 取回失败 / 取回成功 / 空路线。其中失败态尤其重要 ——
 * 行程读不回来时**绝不能**退化成"看起来像成功了"，也不能只留一句"出错了"。
 *
 * 另外钉住三处诚实性标注，它们是这套界面存在的理由：
 * - `transport_source === "estimated"` → 每段交通都要带「估算值」；
 * - `budget_estimated` / `budget_unknown_items` → 金额旁边必须能看到"哪些是估算/未知"；
 * - `route_count_note` → 方案少于 3 套时说出**为什么**，而不是凑数。
 */

import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { TripOut, TripRoute } from "@/lib/api";
import { formatAmount, formatBudget, formatStopWarning, TripResultView } from "@/components/trip-result";

// 这个文件只测**纯展示**（`TripResultView` 与几个格式化函数）：不联网、不需要 mock。
// 「取回行程 + 改路线/撤销/分享」在 `trip-workspace.test.tsx` 里测。

const FULL_ROUTE: TripRoute = {
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
  budget_unknown_items: ["点都德·breakfast", "惠食佳·lunch"],
  recommend_score: 0.97,
  feasibility: {
    feasible: true,
    violations: [],
    warnings: [
      {
        code: "HOURS_UNKNOWN",
        at_seq: 1,
        severity: "soft",
        message: "点都德 营业时间未知，出发前请确认",
      },
      {
        code: "PRICE_UNKNOWN",
        at_seq: null,
        severity: "soft",
        message: "有 2 个项目没有可核验的价格",
      },
    ],
  },
  best_for: ["轻松", "不想多走路"],
  pros: ["路线效率（1.00）"],
  cons: ["点都德 营业时间未知，出发前请确认"],
  recommendation_reason: "从茶楼早茶开始，逛步行街，再到老牌粤菜馆收尾。",
  route_source: "generated",
  template_route_id: null,
  stops: [
    {
      seq: 0,
      place_id: "p1",
      name: "点都德",
      category: "food",
      latitude: 23.12,
      longitude: 113.26,
      district: null,
      arrive_time: "09:00",
      depart_time: "10:30",
      stay_min: 90,
      transport_mode: "walk",
      transport_min: 8,
      transport_distance_m: 537,
      transport_source: "estimated",
      why_recommended: "美食",
      tips: null,
      warnings: ["ESTIMATED_TRANSIT", "SOMETHING_NEW"],
      snapshot: {},
    },
    {
      seq: 1,
      place_id: "p2",
      name: "北京路商业步行街",
      category: "district",
      latitude: 23.12,
      longitude: 113.26,
      district: null,
      arrive_time: "10:38",
      depart_time: "12:38",
      stay_min: 120,
      transport_mode: null,
      transport_min: null,
      transport_distance_m: null,
      transport_source: null,
      why_recommended: null,
      tips: null,
      warnings: [],
      snapshot: {},
    },
  ],
};

/** 一份「什么都没有」的方案：用来覆盖所有"未知"分支。 */
const BARE_ROUTE: TripRoute = {
  ...FULL_ROUTE,
  id: "route-2",
  label: "B",
  archetype: "weird_archetype",
  name: "园林茶点线",
  one_liner: null,
  total_duration_min: 0,
  walking_distance_m: null,
  transit_time_min: null,
  transit_distance_m: null,
  budget_min: null,
  budget_max: null,
  budget_estimated: false,
  budget_unknown_items: [],
  best_for: [],
  pros: [],
  cons: [],
  recommendation_reason: null,
  feasibility: {},
  stops: [],
};

const TRIP: TripOut = {
  trip_id: "trip-1",
  request_id: "req-1",
  city: "guangzhou",
  title: "广州 · 09:00–21:00",
  days: 1,
  revision_no: 1,
  route_count: 2,
  route_count_requested: 3,
  route_count_note: "符合约束的候选路线只有 2 条，没有凑第 3 条。",
  intent: {},
  degraded_modes: ["search:未配置搜索 API Key（只读本地知识库，不联网）"],
  total_cost_cny: "0",
  generation_ms: 225,
  created_at: "2026-09-14T03:24:00+00:00",
  is_public: false,
  share_slug: null,
  routes: [FULL_ROUTE, BARE_ROUTE],
};

// ── 纯函数 ──────────────────────────────────────────────────────────────────

describe("金额与预算的展示", () => {
  it("尾零收敛，但不做浮点换算", () => {
    expect(formatAmount("18.32")).toBe("18.32");
    expect(formatAmount("20.00")).toBe("20");
    expect(formatAmount("20.50")).toBe("20.5");
    // 非数值原样返回：宁可显示得奇怪，也不要猜一个数出来
    expect(formatAmount("many")).toBe("many");
  });

  it("只有下限说「起」，只有上限说「最多」，两端反了说「未知」", () => {
    expect(formatBudget("18.32", "18.32", "per_person")).toBe("¥18.32/人");
    expect(formatBudget("20.00", "50.00", "total")).toBe("¥20–50/总计");
    expect(formatBudget("20", null, "per_person")).toBe("¥20 起/人");
    expect(formatBudget(null, "50", "per_person")).toBe("最多 ¥50/人");
    expect(formatBudget("50", "20", "per_person")).toBe("未知");
    expect(formatBudget(null, null, "per_person")).toBe("未知");
    // 后端以后新增口径时不能硬编成"每人"
    expect(formatBudget("10", "10", "unknown_scope")).toBe("¥10");
  });

  it("校验码译成人话；没见过的码原样返回（好过编一个说法）", () => {
    expect(formatStopWarning("ESTIMATED_TRANSIT")).toBe("耗时与距离为估算值");
    expect(formatStopWarning("HOURS_UNKNOWN")).toBe("营业时间未知，出发前请确认");
    expect(formatStopWarning("BRAND_NEW_CODE")).toBe("BRAND_NEW_CODE");
    // 原型链上的键不能被当成"有翻译"（见 lib/format.ts 的 lookupLabel）
    expect(formatStopWarning("constructor")).toBe("constructor");
  });
});

// ── 纯渲染 ──────────────────────────────────────────────────────────────────

describe("TripResultView", () => {
  it("渲染方案、站点、估算标记与方案的取舍", () => {
    render(<TripResultView trip={TRIP} />);

    expect(screen.getByTestId("trip-result")).toBeInTheDocument();
    expect(screen.getByText("广州 · 09:00–21:00")).toBeInTheDocument();
    expect(screen.getByText(/1 天 · 2 套方案/)).toBeInTheDocument();

    const card = screen.getByTestId("trip-route-A");
    expect(within(card).getByText("老城寻味慢行")).toBeInTheDocument();
    expect(within(card).getByText(/3 站 · 约 5.3 小时/)).toBeInTheDocument();
    expect(within(card).getByText(/从茶楼早茶开始/)).toBeInTheDocument();
    // one_liner 是代码算的，模型文案在下面一段 —— 两者不混
    expect(within(card).getByText(/09:00–10:30/)).toBeInTheDocument();
    expect(within(card).getByText(/→ 步行 8 分 · 537 m/)).toBeInTheDocument();
    expect(within(card).getByText("（估算值）")).toBeInTheDocument();
    expect(within(card).getByText(/¥18.32\/人/)).toBeInTheDocument();
    expect(within(card).getByText("（估算）")).toBeInTheDocument();
    expect(
      within(card).getByText(/未含价格：点都德·breakfast、惠食佳·lunch/),
    ).toBeInTheDocument();
    expect(within(card).getByText("预算含估算值")).toBeInTheDocument();
    expect(within(card).getByText(/适合：轻松 · 不想多走路/)).toBeInTheDocument();
    expect(within(card).getByText(/优点 · 路线效率/)).toBeInTheDocument();
    expect(within(card).getByText(/需要留意 · 点都德/)).toBeInTheDocument();
    // 站点级校验码译成人话；不认识的码原样露出
    expect(within(card).getByText(/耗时与距离为估算值/)).toBeInTheDocument();
    expect(within(card).getByText(/SOMETHING_NEW/)).toBeInTheDocument();
    // at_seq 为 null 的提示是"整条路线"级别的，单独一行
    expect(within(card).getByText(/整条路线：有 2 个项目没有可核验的价格/)).toBeInTheDocument();
    // at_seq 有值的提示已经在站点行上，不再重复一遍
    expect(within(card).queryByText(/整条路线：点都德/)).toBeNull();

    // 降级模式与"方案数不足"的原因都要说清楚
    expect(screen.getByText(/当前降级模式：search:/)).toBeInTheDocument();
    expect(screen.getByText(/没有凑第 3 条/)).toBeInTheDocument();
  });

  it("缺什么就说未知，不编数字也不假装有站点", () => {
    render(<TripResultView trip={{ ...TRIP, routes: [BARE_ROUTE] }} />);

    const card = screen.getByTestId("trip-route-B");
    // 未知枚举原样显示，方便发现后端加了新取值
    expect(within(card).getByText("weird_archetype")).toBeInTheDocument();
    expect(within(card).getByText(/这套方案没有带站点明细/)).toBeInTheDocument();
    expect(within(card).getByText("时长未知")).toBeInTheDocument();
    expect(within(card).getByText("步行距离未知")).toBeInTheDocument();
    expect(within(card).getAllByText("未知").length).toBeGreaterThan(0);
    // 没有估算项时不显示"预算含估算值"这种吓人的标记
    expect(within(card).queryByText("预算含估算值")).toBeNull();
  });

  it("路线列表为空时明说没有，而不是画一份假行程", () => {
    render(
      <TripResultView trip={{ ...TRIP, routes: [], route_count: 0, route_count_note: null }} />,
    );

    expect(screen.getByText(/这次没有生成任何方案/)).toBeInTheDocument();
    expect(screen.queryByTestId("trip-route-A")).toBeNull();
  });

  it("有硬性校验失败时明确标出，不隐藏在成功里", () => {
    render(
      <TripResultView
        trip={{
          ...TRIP,
          routes: [
            {
              ...BARE_ROUTE,
              label: "C",
              id: "route-3",
              feasibility: {
                feasible: false,
                // 三种情况都要能显示：有 message / 只有 code / 什么都没有
                violations: [
                  { code: "WALKING_OVER_LIMIT", at_seq: null, message: "步行超过上限" },
                  { code: "BUDGET_OVER_LIMIT", at_seq: null },
                  {},
                ],
                warnings: [],
              },
            },
          ],
        }}
      />,
    );

    const card = screen.getByTestId("trip-route-C");
    expect(within(card).getByText(/未通过校验：步行超过上限/)).toBeInTheDocument();
    expect(within(card).getByText(/未通过校验：BUDGET_OVER_LIMIT/)).toBeInTheDocument();
    expect(within(card).getByText(/未通过校验：未知问题/)).toBeInTheDocument();
  });
});
