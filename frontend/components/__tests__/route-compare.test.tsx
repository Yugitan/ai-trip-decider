/**
 * 方案对比表（`components/route-compare.tsx`）的测试。
 *
 * 这一栏存在的理由是「让三套方案可比」，所以它最容易犯的错不是崩，而是**说错话**：
 * - 把「后端没给校验结论」说成「通过校验」—— 用户会以为这套方案已经验过了；
 * - 把缺的数据写成 0、`—` 或空白 —— 看起来像一个正常数值；
 * - 同一份数据在表格里和卡片里长得不一样 —— 两处都"是真的"，用户不知道该信哪个（
 *   所以两处都调用 `lib/format` 的同一个函数，这里断言的就是那个函数的输出）；
 * - 顺手替用户做决定（"B 最好"）—— 表格只对齐事实。
 *
 * 读取单元格一律走 `textContent`：单元格里是「数字 + 估算标记」两个节点拼起来的，
 * `getByText` 只看元素自己的文本节点，会看不到拼接后的那一句话。
 */

import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { TripRoute } from "@/lib/api";
import { RouteCompare, routeCompare } from "@/components/route-compare";

/** 三套方案各占一个极端：A 数据齐全、B 几乎什么都没有、C 有硬性失败。 */
const BASE: TripRoute = {
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
    warnings: [{ code: "HOURS_UNKNOWN", at_seq: 1, message: "营业时间未知" }],
  },
  best_for: ["轻松", "不想多走路"],
  pros: ["路线效率（1.00）"],
  cons: ["点都德 营业时间未知，出发前请确认"],
  recommendation_reason: "从茶楼早茶开始。",
  route_source: "generated",
  template_route_id: null,
  stops: [],
};

const ROUTE_A = BASE;

/** 数据缺口最多的一套：可选字段全空，用来钉住"未知"的写法。 */
const ROUTE_B: TripRoute = {
  ...BASE,
  id: "route-2",
  label: "B",
  archetype: "classic",
  name: "园林茶点线",
  total_duration_min: 0,
  walking_distance_m: null,
  transit_time_min: null,
  transit_distance_m: null,
  budget_min: null,
  budget_max: null,
  budget_scope: null,
  budget_estimated: false,
  budget_unknown_items: [],
  feasibility: {},
  best_for: [],
  cons: [],
  recommend_score: 0.5,
};

/** 硬性校验失败：`feasible: false`，且一条 violations 都没有（最容易被糊过去的形态）。 */
const ROUTE_C: TripRoute = {
  ...BASE,
  id: "route-3",
  label: "C",
  archetype: "themed",
  theme: "美食",
  name: "西关甜味线",
  feasibility: { feasible: false, violations: [], warnings: [] },
  recommend_score: 0.8,
};

/** 取某一行的三列文本（按行标题找到那一行，不用下标猜列）。 */
function cellTexts(label: string): string[] {
  const row = screen.getByRole("rowheader", { name: label }).closest("tr");
  if (row === null) throw new Error(`没找到「${label}」这一行`);
  return Array.from(row.querySelectorAll("td")).map((cell) => cell.textContent ?? "");
}

describe("routeCompare（纯函数）", () => {
  it("每个单元格都带着自己的方案 id，列的配对不靠下标", () => {
    const rows = routeCompare([ROUTE_A, ROUTE_B]);

    expect(rows.map((row) => row.label)).toEqual([
      "方案名称",
      "地点",
      "总时长",
      "步行",
      "交通",
      "预算",
      "适合",
      "需要留意",
      "校验结果",
    ]);
    for (const row of rows) {
      expect(row.cells.map((cell) => cell.routeId)).toEqual(["route-1", "route-2"]);
    }
  });

  it("零套方案不会造出没有值的行", () => {
    expect(routeCompare([]).every((row) => row.cells.length === 0)).toBe(true);
  });

  it("★ 缺结论时不说「通过校验」", () => {
    const row = routeCompare([ROUTE_B]).find((item) => item.label === "校验结果");
    expect(row?.cells[0]?.text).toBe("校验结论未给出");
    expect(row?.cells[0]?.severe).toBeUndefined();
  });

  it("★ `feasible: false` 但没有 violations 时，结论仍是「未通过」", () => {
    const row = routeCompare([ROUTE_C]).find((item) => item.label === "校验结果");
    expect(row?.cells[0]?.text).toBe("未通过校验");
    expect(row?.cells[0]?.severe).toBe(true);
  });

  it("有几条 violations 就报几条，并用警示标记", () => {
    const row = routeCompare([
      {
        ...ROUTE_A,
        feasibility: {
          feasible: false,
          violations: [{ code: "WALKING_OVER_LIMIT" }, { code: "BUDGET_OVER_LIMIT" }],
        },
      },
    ]).find((item) => item.label === "校验结果");

    expect(row?.cells[0]?.text).toBe("2 项未通过校验");
    expect(row?.cells[0]?.severe).toBe(true);
  });

  it("通过但有提醒时，把提醒条数说出来（不把提醒吞掉）", () => {
    const row = routeCompare([ROUTE_A]).find((item) => item.label === "校验结果");
    expect(row?.cells[0]?.text).toBe("通过校验 · 1 条提醒");
    expect(row?.cells[0]?.severe).toBeUndefined();
  });
});

describe("RouteCompare", () => {
  it("三套方案逐项对齐：表头按方案分列，每一行是一个可比指标", () => {
    render(<RouteCompare routes={[ROUTE_A, ROUTE_B, ROUTE_C]} />);

    const table = screen.getByRole("table");
    // 一列是行标题，其余每套方案一列
    expect(within(table).getAllByRole("columnheader")).toHaveLength(4);
    expect(screen.getByText("方案 A")).toBeInTheDocument();
    expect(screen.getByText("方案 B")).toBeInTheDocument();
    expect(screen.getByText("方案 C")).toBeInTheDocument();
    // 表头给的是枚举的中文说法，不是 `relaxed` 这种机器码
    expect(screen.getByText("轻松休闲")).toBeInTheDocument();
    expect(screen.getByText("主题型")).toBeInTheDocument();

    expect(cellTexts("地点")).toEqual(["3 个", "3 个", "3 个"]);
    expect(cellTexts("总时长")).toEqual(["5 小时 17 分", "时长未知", "5 小时 17 分"]);
    expect(cellTexts("步行")).toEqual(["537 m", "步行距离未知", "537 m"]);
    expect(cellTexts("交通")).toEqual(["17 分钟 · 3.0 km", "未知", "17 分钟 · 3.0 km"]);
  });

  it("★ 金额与卡片同一口径，估算与价格缺口都跟着数字走", () => {
    render(<RouteCompare routes={[ROUTE_A, ROUTE_B]} />);

    expect(cellTexts("预算")).toEqual(["¥18.32/人（未含 2 项价格）（估算）", "未知"]);
    // 两端都没有价格时不许出现 ¥0 这种"看起来是免费"的写法
    expect(cellTexts("预算").some((text) => text.includes("¥0"))).toBe(false);
  });

  it("「适合」为空说未给出，「需要留意」为空说未列出", () => {
    render(<RouteCompare routes={[ROUTE_A, ROUTE_B]} />);

    expect(cellTexts("适合")).toEqual(["轻松 · 不想多走路", "未给出"]);
    expect(cellTexts("需要留意")).toEqual([
      "点都德 营业时间未知，出发前请确认",
      "未列出",
    ]);
  });

  it("主题只在有值时才跟在名称后面", () => {
    render(<RouteCompare routes={[ROUTE_A, ROUTE_C]} />);

    expect(cellTexts("方案名称")).toEqual(["老城寻味慢行", "西关甜味线（美食）"]);
  });

  it("★ 没有占位符单元格：不知道就说不知道，不写「—」「无」或留空", () => {
    render(<RouteCompare routes={[ROUTE_A, ROUTE_B, ROUTE_C]} />);

    const texts = screen.getAllByRole("cell").map((cell) => (cell.textContent ?? "").trim());
    expect(texts).not.toContain("—");
    expect(texts).not.toContain("-");
    expect(texts).not.toContain("无");
    expect(texts.every((text) => text !== "")).toBe(true);
    // 未知必须出现（B 有五个字段是空的），避免"看起来全都有数据"
    expect(texts.filter((text) => text.includes("未知")).length).toBeGreaterThan(0);
  });

  it("★ 只做对齐、不做推荐：表里不许出现排名或\"首选\"字样", () => {
    render(<RouteCompare routes={[ROUTE_A, ROUTE_B, ROUTE_C]} />);

    // recommend_score 是 7 维权重算出来的，一张表解释不了它 —— 放出来只会变成"按分数选"
    for (const score of ["0.97", "0.5", "0.8"]) {
      expect(screen.queryByText(new RegExp(score.replace(".", "\\.")))).toBeNull();
    }
    expect(screen.queryByText(/最佳|首选|第一名|推荐指数/)).toBeNull();
    // 但要明确告诉用户这一栏不替他做决定
    expect(screen.getByText(/不排名、不替你选/)).toBeInTheDocument();
  });

  it("一套方案时不渲染表格（一列的表会让人以为其余方案被排除了）", () => {
    const { container } = render(<RouteCompare routes={[ROUTE_A]} />);
    expect(container).toBeEmptyDOMElement();

    const { container: empty } = render(<RouteCompare routes={[]} />);
    expect(empty).toBeEmptyDOMElement();
  });

  it("表格自带说明与行标题语义（每列是一套方案、每行是一个对比项）", () => {
    render(<RouteCompare routes={[ROUTE_A, ROUTE_B]} />);

    expect(screen.getByText(/每一列是一套方案，每一行是一个对比项/)).toBeInTheDocument();
    // 行标题用 th[scope=row]，屏幕阅读器才知道"这个值属于哪一项"
    expect(screen.getAllByRole("rowheader")).toHaveLength(9);
    expect(screen.getByRole("columnheader", { name: "对比项" })).toBeInTheDocument();
  });
});
