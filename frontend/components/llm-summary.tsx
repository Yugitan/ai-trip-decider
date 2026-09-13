"use client";

import type { LlmStatus } from "@/lib/api";

/**
 * 把 `meta.llm` 翻译成人话。
 *
 * 三条原则（与后端 `llm_planner` 的边界一一对应）：
 * 1. **不美化降级**：没配 Key、调用失败、命中缓存都如实说，不用"AI 已优化"糊过去；
 * 2. **金额带口径**：未校准的单价必须标出来（`cost_calibrated=false`），
 *    否则那个数字看起来像真实支出；
 * 3. **不展示 Key**：这里只显示模型名与用量，任何密钥都不会到前端。
 */

const TASK_LABELS: Record<string, string> = {
  intent_patch: "意图补全",
  route_narrative: "方案文案",
};

export function describeTaskSource(source: string): string {
  if (source === "llm") return "模型";
  if (source === "cache") return "模型（命中缓存）";
  if (source === "rule") return "规则引擎";
  return source;
}

export function taskSummary(tasks: Record<string, string>): string {
  const entries = Object.entries(tasks).filter(([key]) => key !== "result");
  if (entries.length === 0) return "本次未调用";
  return entries
    .map(([key, source]) => `${TASK_LABELS[key] ?? key}：${describeTaskSource(source)}`)
    .join(" · ");
}

export function LlmSummary({ llm }: { llm: LlmStatus | null }) {
  if (llm === null) {
    return (
      <p className="text-xs leading-relaxed text-ink-soft">
        这次响应没有带上模型使用信息（后端未返回 <code>meta.llm</code>）。
      </p>
    );
  }

  return (
    <div data-testid="llm-summary" className="rounded-[10px] border border-line bg-shell/70 px-3 py-2.5">
      <p className="text-xs font-medium text-ink">
        {llm.used ? "本次由模型参与" : "本次用规则引擎完成"}
      </p>

      <p className="mt-1 text-xs leading-relaxed text-ink-soft">
        {llm.enabled
          ? `已配置 ${llm.provider}${llm.model === null ? "" : `（${llm.model}）`}`
          : "未配置 LLM API Key —— 用规则引擎 + 模板文案兜底，功能不受影响"}
      </p>

      <p className="tnum mt-1 text-xs leading-relaxed text-ink-soft">
        任务：{taskSummary(llm.tasks)}
      </p>

      {llm.used || llm.cache_hits > 0 ? (
        <p className="tnum mt-1 text-xs leading-relaxed text-ink-soft">
          调用 {llm.calls} 次
          {llm.cache_hits > 0 ? ` · 缓存命中 ${llm.cache_hits} 次（未重复计费）` : ""}
          {llm.calls > 0 ? ` · token ${llm.tokens_in} 入 / ${llm.tokens_out} 出` : ""}
        </p>
      ) : null}

      {llm.used ? (
        <p className="tnum mt-1 text-xs leading-relaxed text-ink-soft">
          成本约 ¥{llm.cost_cny}
          {llm.cost_calibrated ? "（单价已校准）" : "（单价未校准，数字只是量级参考）"}
        </p>
      ) : null}

      {llm.fallback_reasons.length > 0 ? (
        <ul className="mt-1.5 space-y-0.5">
          {llm.fallback_reasons.map((reason) => (
            <li key={reason} className="text-xs leading-relaxed text-coral">
              已降级：{reason}
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
