"use client";

import { useCallback, useEffect, useState } from "react";

import { ApiError, getCostSummary, type CostSummary } from "@/lib/api";
import { readAdminToken, saveAdminToken } from "@/lib/dev-api";

/**
 * 成本后台（PRD FR-12 AC-12.2 / §25）。
 *
 * ★ 这一页存在的意义是\"数字能被相信\" ★
 * 因此它遵守与报告生成器（`scripts/cost_report.py`）同样的三条纪律：
 *
 * 1. **未校准的金额必须显式标注**：单价为 `null` 时金额记 0 但 `calibrated=False`，
 *    这时\"低于红线\"是一句没有根据的话 —— 后端在这种情况下回 `within_target: null`，
 *    界面必须显示\"无法判断\"，而不是一个绿色的 ✅。
 * 2. **单次规划成本按 `request_id` 聚合**（后端做的），不是拿 `cost_logs` 的行平均。
 *    所以这里显示的是 `plans`（笔数）而不是 `calls`（调用次数）。
 * 3. **阈值来自后端**（`limits.yaml`）：页面不自己写死 ¥0.5，
 *    否则改了配置、页面还报着旧数字（TASKS 问题 73 就是同一类错误）。
 *
 * ★ Token 的存放与 `/dev` 面板共用一套（`lib/dev-api.ts` 的 sessionStorage）★
 * 不另起一个键、也不存 cookie：它不是会话凭证，只是一个临时口令，
 * 关掉标签页就该消失。两处各存一份的结果是"在面板里填过、成本页里还要再填一次"。
 * 配了 `ADMIN_TOKEN` 时后端要求 `X-Admin-Token`，没配时只接受本机来源。
 */

const WINDOWS = [1, 7, 30, 90] as const;

function percent(value: number | null): string {
  if (value === null) return "未知";
  return `${(value * 100).toFixed(1)}%`;
}

/** 金额：后端给的是**数字**（这里不是 Decimal 字符串），保留 4 位小数够看单次成本。 */
function money(value: number | null): string {
  if (value === null) return "未知";
  return `¥${value.toFixed(4)}`;
}

export function AdminCostDashboard() {
  const [token, setToken] = useState<string>("");
  const [days, setDays] = useState<number>(7);
  const [data, setData] = useState<CostSummary | null>(null);
  const [error, setError] = useState<{ message: string; hint: string; code: string } | null>(
    null,
  );
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    // 首帧渲染空值、挂载后才读存储：服务端渲染读不到 sessionStorage，
    // 直接在 useState 里读会让服务端与客户端的 HTML 不一致（hydration 警告）。
    setToken(readAdminToken());
  }, []);

  const handleLoad = useCallback(
    async (nextDays: number, providedToken: string) => {
      setLoading(true);
      setError(null);
      try {
        const result = await getCostSummary(nextDays, providedToken.trim() || null);
        setData(result.data);
        if (providedToken.trim()) saveAdminToken(providedToken.trim());
      } catch (cause) {
        setData(null);
        if (cause instanceof ApiError) {
          setError({ message: cause.message, hint: cause.hint, code: cause.code });
        } else {
          setError({
            message: "无法连接到后端服务",
            hint: "确认后端已启动（make dev-backend）。",
            code: "NETWORK",
          });
        }
      } finally {
        setLoading(false);
      }
    },
    [],
  );

  useEffect(() => {
    void handleLoad(days, token);
    // token 的变化由\"读取\"按钮触发，避免每敲一个字就打一次接口
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [days]);

  return (
    <div className="space-y-6">
      <form
        className="flex flex-wrap items-end gap-3 rounded-card border border-line bg-shell p-5"
        onSubmit={(event) => {
          event.preventDefault();
          void handleLoad(days, token);
        }}
      >
        <label className="flex flex-1 flex-col gap-1.5 text-xs text-ink-soft">
          后台 Token（未设置 ADMIN_TOKEN 时留空，限本机访问）
          <input
            type="password"
            value={token}
            onChange={(event) => setToken(event.target.value)}
            data-testid="admin-token"
            autoComplete="off"
            className="min-h-11 rounded-btn border border-line bg-sand px-3 text-sm text-ink"
          />
        </label>

        <label className="flex flex-col gap-1.5 text-xs text-ink-soft">
          统计窗口
          <select
            value={String(days)}
            onChange={(event) => setDays(Number(event.target.value))}
            data-testid="admin-days"
            className="min-h-11 rounded-btn border border-line bg-sand px-3 text-sm text-ink"
          >
            {WINDOWS.map((option) => (
              <option key={option} value={option}>
                近 {option} 天
              </option>
            ))}
          </select>
        </label>

        <button
          type="submit"
          data-testid="admin-load"
          className="inline-flex min-h-11 items-center justify-center rounded-full bg-ink px-6 text-sm text-sand transition-transform duration-300 hover:scale-[1.03] motion-reduce:hover:scale-100"
        >
          读取
        </button>
      </form>

      {loading ? (
        <p data-testid="admin-loading" className="text-xs text-ink-soft">
          正在读取成本数据…
        </p>
      ) : null}

      {error === null ? null : (
        <div
          data-testid="admin-error"
          className="rounded-card border border-coral/40 bg-coral-tint/60 p-5"
        >
          <p className="text-sm font-medium text-ink">{error.message}</p>
          <p className="mt-1 text-xs leading-relaxed text-ink-soft">{error.hint}</p>
          <p className="tnum mt-1 text-xs text-ink-faint">错误码：{error.code}</p>
        </div>
      )}

      {data === null || error !== null ? null : (
        <>
          <section className="rounded-card border border-line bg-shell p-5">
            <h2 className="text-lg text-ink">单次规划成本（近 {data.window_days} 天）</h2>
            <p className="mt-1 text-xs leading-relaxed text-ink-soft">
              按一次规划（<code className="text-ink">request_id</code>）聚合，不是按调用次数
              —— PRD §3.2 的红线说的是「一次规划」。
            </p>
            <dl className="mt-3 grid grid-cols-2 gap-3 sm:grid-cols-4">
              {[
                { label: "规划笔数", value: String(data.plans.plans) },
                { label: "平均", value: money(data.plans.avg_cny) },
                { label: "P50", value: money(data.plans.p50_cny) },
                { label: "P95", value: money(data.plans.p95_cny) },
              ].map((item) => (
                <div key={item.label} className="rounded-[10px] border border-line bg-sand p-3">
                  <dt className="text-xs text-ink-soft">{item.label}</dt>
                  <dd className="tnum mt-1 text-lg text-ink">{item.value}</dd>
                </div>
              ))}
            </dl>
            <p className="mt-3 text-xs leading-relaxed text-ink-soft">
              红线 ¥{data.plans.target_cny}/次：
              {data.plans.within_target === true ? (
                <span className="text-teal-dark"> 达标</span>
              ) : data.plans.within_target === false ? (
                <span className="text-coral"> 超标</span>
              ) : (
                // 未校准的窗口里\"低于红线\"是一句没有根据的话
                <span className="text-ink"> 无法判断（窗口内有未校准记录）</span>
              )}
            </p>
            {data.plans.rows_without_request > 0 ? (
              <p className="tnum mt-1 text-xs leading-relaxed text-ink-faint">
                另有 {data.plans.rows_without_request} 条调用没有 request_id（脚本或早期数据），
                不计入上面的平均值。
              </p>
            ) : null}
          </section>

          <section className="rounded-card border border-line bg-shell p-5">
            <h2 className="text-lg text-ink">成本与缓存</h2>
            <dl className="mt-3 grid grid-cols-2 gap-3 sm:grid-cols-4">
              <div className="rounded-[10px] border border-line bg-sand p-3">
                <dt className="text-xs text-ink-soft">调用次数</dt>
                <dd className="tnum mt-1 text-lg text-ink">{data.cache.calls}</dd>
              </div>
              <div className="rounded-[10px] border border-line bg-sand p-3">
                <dt className="text-xs text-ink-soft">缓存命中率</dt>
                <dd className="tnum mt-1 text-lg text-ink">{percent(data.cache.hit_rate)}</dd>
              </div>
              <div className="rounded-[10px] border border-line bg-sand p-3">
                <dt className="text-xs text-ink-soft">今日支出</dt>
                <dd className="tnum mt-1 text-lg text-ink">{data.daily.spent_cny}</dd>
              </div>
              <div className="rounded-[10px] border border-line bg-sand p-3">
                <dt className="text-xs text-ink-soft">日上限</dt>
                <dd className="tnum mt-1 text-lg text-ink">{data.daily.limit_cny}</dd>
              </div>
            </dl>
            <p className="mt-3 text-xs leading-relaxed text-ink-soft">
              日预算{data.daily.exceeded ? "已用完，规划进入缓存优先模式" : "未用完"}。
              熔断：单次 ¥{data.breakers.plan_total_cny} / 搜索 ¥{data.breakers.plan_search_cny} /
              LLM {data.breakers.plan_llm_calls} 次 / 地图 {data.breakers.plan_map_calls} 次。
            </p>
            <p
              data-testid="admin-pricing-note"
              className={`mt-2 text-xs leading-relaxed ${
                data.pricing_calibrated ? "text-ink-soft" : "text-coral"
              }`}
            >
              {data.note}
              {data.uncalibrated_rows > 0 ? `（未校准记录 ${data.uncalibrated_rows} 条）` : ""}
            </p>
          </section>

          <section className="rounded-card border border-line bg-shell p-5">
            <h2 className="text-lg text-ink">按类别</h2>
            <table className="mt-3 w-full text-left text-sm">
              <thead className="text-xs text-ink-soft">
                <tr>
                  <th className="py-1.5">类别</th>
                  <th className="py-1.5">调用</th>
                  <th className="py-1.5">缓存命中</th>
                  <th className="py-1.5">金额</th>
                </tr>
              </thead>
              <tbody className="tnum">
                {data.by_category.length === 0 ? (
                  <tr>
                    <td colSpan={4} className="py-2 text-xs text-ink-soft">
                      窗口内没有任何调用记录。
                    </td>
                  </tr>
                ) : (
                  data.by_category.map((row) => (
                    <tr key={row.category} className="border-t border-line/70">
                      <td className="py-1.5">{row.category}</td>
                      <td className="py-1.5">{row.calls}</td>
                      <td className="py-1.5">{row.cache_hits}</td>
                      <td className="py-1.5">¥{row.amount_cny}</td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </section>

          <section className="rounded-card border border-line bg-shell p-5">
            <h2 className="text-lg text-ink">最贵的几笔</h2>
            {data.plans.top.length === 0 ? (
              <p className="mt-2 text-xs text-ink-soft">窗口内还没有带 request_id 的规划。</p>
            ) : (
              <ul data-testid="admin-top-plans" className="mt-3 space-y-1.5 text-sm">
                {data.plans.top.map((row) => (
                  <li key={row.request_id} className="tnum flex flex-wrap items-baseline gap-x-3">
                    <span className="break-all text-ink">{row.request_id}</span>
                    <span className="text-ink-soft">¥{row.amount_cny}</span>
                    <span className="text-xs text-ink-faint">{row.calls} 次调用</span>
                    {row.uncalibrated ? (
                      <span className="text-xs text-coral">含未校准单价</span>
                    ) : null}
                  </li>
                ))}
              </ul>
            )}
          </section>
        </>
      )}
    </div>
  );
}
