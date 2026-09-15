"use client";

import type { TripRoute } from "@/lib/api";
import {
  formatArchetype,
  formatBudget,
  formatDistance,
  formatDuration,
  formatTransit,
} from "@/lib/format";

/**
 * 三套方案的**交叉对比表**：同一份需求、同一批指标，逐项对齐。
 *
 * 为什么需要它：三套方案此前只有并列卡片 —— 想比较"哪套走得少""哪套贵"，
 * 得在几百行卡片之间来回找同一行。这一栏把可比项排成表，**先看差异，再看细节**。
 *
 * ★ 四条纪律（与结果卡片完全一致，同一份数据在两处不能说成两个样子）★
 * 1. **只做对齐，不做推荐**：不排名、不写"最佳/首选"，`recommend_score` 也不在这里露出 ——
 *    分数怎么来的（7 维权重 + 乘数）不是一张表能解释清楚的，放出来只会变成"看分数选"；
 * 2. **缺数据就说未知**：一律走 `lib/format`，绝不用 0 或"—"冒充一个正常数值；
 * 3. **估算与硬性失败必须可见**：`budget_estimated` 带「（估算）」，
 *    `violations` / `feasible === false` 用警示色并写明几项未通过；
 * 4. **少一套方案就不画表**：只有一列的表不叫对比，不如让卡片自己说。
 *
 * 列的配对不靠下标：每个单元格都带着自己的 `routeId`，渲染时按它做 key，
 * 于是"第几列是哪套方案"由构造保证，而不是"别搞错顺序"。
 */

/** 一个对比单元格。`text` 一定是**给用户看的话**，不是枚举或机器码。 */
export interface CompareCell {
  /** 这个值属于哪套方案（与表头按 id 配对，不靠下标猜） */
  routeId: string;
  text: string;
  /** 该值含估算成分 —— 展示时必须带标记（与卡片里的「（估算）」同一口径） */
  estimated?: boolean;
  /** 该值是硬性校验失败 —— 展示时用警示色 */
  severe?: boolean;
}

export interface CompareRow {
  /** 行标题（左侧那一列） */
  label: string;
  /** 与传入的 `routes` 一一对应 */
  cells: CompareCell[];
}

/** 名称与主题合成一行：主题为空时只说名称，不写「主题：无」。 */
function titleCell(route: TripRoute): Omit<CompareCell, "routeId"> {
  return { text: route.theme ? `${route.name}（${route.theme}）` : route.name };
}

/**
 * 预算 → 人话 + 缺口条数。
 *
 * 「未含 N 项价格」是刻意保留的：金额旁边不写清楚它漏了什么，
 * 那个数字就会被当成"这套方案就是这个价"。
 */
function budgetCell(route: TripRoute): Omit<CompareCell, "routeId"> {
  const unknownItems = route.budget_unknown_items ?? [];
  const gap = unknownItems.length > 0 ? `（未含 ${unknownItems.length} 项价格）` : "";
  return {
    text: `${formatBudget(route.budget_min, route.budget_max, route.budget_scope)}${gap}`,
    estimated: route.budget_estimated === true,
  };
}

/**
 * 适合谁。空数组**不是**「不知道」而是「后端没给出」—— 两者说法不同：
 * 前者我们会去查，后者只能照实说没给。
 */
function bestForCell(route: TripRoute): Omit<CompareCell, "routeId"> {
  const bestFor = route.best_for ?? [];
  return { text: bestFor.length > 0 ? bestFor.join(" · ") : "未给出" };
}

/** 需要留意：保留原文（卡片里有同样的句子），只在这里压成一行。 */
function caveatsCell(route: TripRoute): Omit<CompareCell, "routeId"> {
  const cons = route.cons ?? [];
  return { text: cons.length > 0 ? cons.join("；") : "未列出" };
}

/**
 * 校验结果。
 *
 * 四种情况分开说，其中两种最容易糊过去：
 * - `feasible === false` 但 `violations` 是空的 → 仍然必须报「未通过」，不能因为数不出
 *   几条就把结论翻成"通过"；
 * - `feasible` 不是 `true`（缺字段 / 后端没给结论）→ 说「未给出」，
 *   **不报「通过」** —— 没校验过就宣称通过，正是这个项目最不该做的事。
 */
function feasibilityCell(route: TripRoute): Omit<CompareCell, "routeId"> {
  const feasibility = route.feasibility;
  const violations = feasibility?.violations ?? [];

  if (feasibility?.feasible === false || violations.length > 0) {
    return {
      text: violations.length > 0 ? `${violations.length} 项未通过校验` : "未通过校验",
      severe: true,
    };
  }
  if (feasibility?.feasible !== true) {
    return { text: "校验结论未给出" };
  }
  const warnings = feasibility.warnings ?? [];
  return {
    text: warnings.length > 0 ? `通过校验 · ${warnings.length} 条提醒` : "通过校验",
  };
}

/**
 * 对比表的行定义：**每行一个指标**，值由同一个纯函数从单套方案算出来。
 * 顺序即表格从上到下的顺序（"值不值得去"的维度排在前面）。
 */
const ROWS: readonly {
  label: string;
  of: (route: TripRoute) => Omit<CompareCell, "routeId">;
}[] = [
  { label: "方案名称", of: titleCell },
  { label: "地点", of: (route) => ({ text: `${route.place_count} 个` }) },
  { label: "总时长", of: (route) => ({ text: formatDuration(route.total_duration_min) }) },
  { label: "步行", of: (route) => ({ text: formatDistance(route.walking_distance_m) }) },
  {
    label: "交通",
    of: (route) => ({
      text: formatTransit(route.transit_time_min, route.transit_distance_m),
    }),
  },
  { label: "预算", of: budgetCell },
  { label: "适合", of: bestForCell },
  { label: "需要留意", of: caveatsCell },
  { label: "校验结果", of: feasibilityCell },
];

/**
 * 把几套方案摊成对比行。纯函数：给定同一个 `routes`，输出恒定 ——
 * 表格里的每个字都能在单测里直接断言，不需要渲染组件。
 */
export function routeCompare(routes: readonly TripRoute[]): CompareRow[] {
  return ROWS.map((row) => ({
    label: row.label,
    cells: routes.map((route) => ({ routeId: route.id, ...row.of(route) })),
  }));
}

function CompareCellView({ cell }: { cell: CompareCell }) {
  return (
    <td className="border-b border-line/60 px-2 py-2.5 align-top leading-relaxed text-ink-soft">
      <span className={cell.severe === true ? "text-coral" : undefined}>{cell.text}</span>
      {cell.estimated === true ? <span className="text-coral">（估算）</span> : null}
    </td>
  );
}

export function RouteCompare({ routes }: { routes: readonly TripRoute[] }) {
  // 一套方案没有可对比的东西。返回 null 而不是画一张单列表：
  // 一张只有一列的表会让人以为"其余方案都被排除了"。
  if (routes.length < 2) return null;

  const rows = routeCompare(routes);

  return (
    <section
      data-testid="route-compare"
      aria-labelledby="route-compare-title"
      className="mt-4 rounded-[10px] border border-line bg-shell/60 p-4"
    >
      <h4 id="route-compare-title" className="text-sm font-medium text-ink">
        先看差异：{routes.length} 套方案逐项对比
      </h4>
      <p className="mt-1 text-xs leading-relaxed text-ink-soft">
        同一份需求下的取舍差异，逐项对齐。数字都由代码算，未知与估算都标出来。这一栏只做对齐 ——
        不排名、不替你选，每套方案自己的说明在下面的卡片里。
      </p>

      <div className="mt-3 overflow-x-auto">
        <table className="w-full min-w-[560px] border-collapse text-xs">
          <caption className="sr-only">
            方案逐项对比表：每一列是一套方案，每一行是一个对比项
          </caption>
          <thead>
            <tr>
              <th
                scope="col"
                className="border-b border-line px-2 py-2 text-left align-bottom font-normal text-ink-faint whitespace-nowrap"
              >
                对比项
              </th>
              {routes.map((route) => (
                <th
                  key={route.id}
                  scope="col"
                  className="border-b border-line px-2 py-2 text-left align-bottom"
                >
                  <span className="block font-medium text-ink">方案 {route.label}</span>
                  <span className="mt-0.5 block font-normal text-ink-faint">
                    {formatArchetype(route.archetype)}
                  </span>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.label}>
                <th
                  scope="row"
                  className="border-b border-line/60 px-2 py-2.5 text-left align-top font-normal text-ink-faint whitespace-nowrap"
                >
                  {row.label}
                </th>
                {row.cells.map((cell) => (
                  <CompareCellView key={cell.routeId} cell={cell} />
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <p className="mt-2 text-[11px] leading-relaxed text-ink-faint">
        表里只有可比项；完整的站点明细、优缺点与来源在下方每张方案卡片里。
      </p>
    </section>
  );
}
