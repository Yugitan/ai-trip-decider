"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";

import { LlmSummary } from "@/components/llm-summary";
import {
  subscribeToPlan,
  type PlanCompletedEvent,
  type PlanFailedEvent,
  type PlanProgressEvent,
} from "@/lib/plan-stream";

/**
 * 生成过程视图（PRD §5.3 / §7.1 `/plan/[requestId]`）。
 *
 * ★ 四个阶段按**真实后端事件**推进 ★
 * 阶段文案与进度都来自 `plan.progress` 事件；在事件到达之前只显示\"等待\"，
 * 不做\"跑三秒假动画\"。这不是性能问题，是诚实问题：一个假进度条在卡住的时候
 * 会一直骗人，而真进度会停在那里 —— 停住本身就是一个可诊断的信号。
 *
 * ★ 超时说的是实话 ★
 * 8s / 20s 两条线来自 PRD §5.3：
 * - 8s 未完成 → 说明\"正在深度校验\"（而不是换个动画继续转）；
 * - 20s 未完成 → 提示可以刷新去拿已经生成的快速方案（后端可能已降级返回）。
 * 我们**不会**在客户端假装已经完成。
 *
 * ★ 完成后不自动跳转，给一个明确的入口 ★
 * `plan.completed` 事件里带着\"这次模型干了什么\"（`meta.llm`），
 * 而结果页读的是 `GET /trips/{id}`，**没有**这份信息。立刻跳走会让用户看不到它，
 * 所以这里先把摘要摆出来，由用户点进结果页。
 */

/** 流水线的四个阶段（顺序固定；文案取自 PRD §5.1）。 */
const STAGES: { key: string; label: string }[] = [
  { key: "intent", label: "理解你的偏好" },
  { key: "candidates", label: "筛选候选地点" },
  { key: "validate", label: "校验路线可行性" },
  { key: "compose", label: "生成方案" },
];

/** 超过这条线仍未完成 → 说明\"正在深度校验\"（PRD §5.3）。 */
const DEEP_CHECK_S = 8;
/** 超过这条线仍未完成 → 提示可以刷新去拿快速方案（PRD §5.3）。 */
const SLOW_S = 20;

/**
 * 把事件里的 `detail` 翻译成一句可核对的话。
 *
 * 只认**已知的键**：事件载荷是不可信输入，遇到不认识的键就不说 ——
 * 编一句\"已筛选 47 个地点\"很容易，但那是把后端没说的事替它说了。
 */
export function describeDetail(detail: Record<string, unknown>): string | null {
  const parts: string[] = [];
  const number = (value: unknown): number | null =>
    typeof value === "number" && Number.isFinite(value) ? value : null;

  const candidates = number(detail["candidates"]);
  if (candidates !== null) parts.push(`候选 ${candidates} 个`);
  const checked = number(detail["checked"]);
  if (checked !== null) parts.push(`已校验 ${checked} 条`);
  const rejected = number(detail["rejected"]);
  if (rejected !== null) parts.push(`剔除 ${rejected} 条`);
  const places = number(detail["places"]);
  if (places !== null) parts.push(`地点 ${places} 个`);

  const reasons = detail["reasons"];
  if (Array.isArray(reasons)) {
    const named = reasons
      .map((item) =>
        typeof item === "object" && item !== null
          ? (item as { code?: unknown }).code
          : null,
      )
      .filter((code): code is string => typeof code === "string");
    if (named.length > 0) parts.push(`原因：${named.join("、")}`);
  }

  return parts.length === 0 ? null : parts.join(" · ");
}

export function PlanProgress({ requestId }: { requestId: string }) {
  const [progress, setProgress] = useState<PlanProgressEvent | null>(null);
  const [completed, setCompleted] = useState<PlanCompletedEvent | null>(null);
  const [failed, setFailed] = useState<PlanFailedEvent | null>(null);
  const [disconnected, setDisconnected] = useState(false);
  const [elapsedS, setElapsedS] = useState(0);

  const streamUrl = useMemo(
    () => `/api/v1/trips/${encodeURIComponent(requestId)}/stream`,
    [requestId],
  );

  useEffect(() => {
    return subscribeToPlan(streamUrl, {
      onProgress: (event) => setProgress(event),
      onCompleted: (event) => setCompleted(event),
      onFailed: (event) => setFailed(event),
      onTransportError: () => setDisconnected(true),
    });
  }, [streamUrl]);

  const settled = completed !== null || failed !== null;

  // 计时只在\"还没落定\"时走：落定之后那个数字已经没有意义了。
  useEffect(() => {
    if (settled) return;
    const timer = setInterval(() => setElapsedS((value) => value + 1), 1000);
    return () => clearInterval(timer);
  }, [settled]);

  if (failed !== null) {
    return (
      <div
        data-testid="plan-failed"
        className="rounded-card border border-coral/40 bg-coral-tint/60 p-6"
      >
        <h2 className="text-lg text-ink">这次规划没有完成</h2>
        <p className="mt-2 text-sm leading-relaxed text-ink-soft">{failed.message}</p>
        {failed.hint === "" ? null : (
          <p className="mt-1 text-xs leading-relaxed text-ink-soft">提示：{failed.hint}</p>
        )}
        <p className="tnum mt-2 text-xs text-ink-faint">错误码：{failed.code}</p>
        <Link
          href="/#planner"
          className="mt-4 inline-flex min-h-11 items-center justify-center rounded-full bg-ink px-6 text-sm text-sand"
        >
          改一下需求再试
        </Link>
      </div>
    );
  }

  if (completed !== null) {
    return (
      <div
        data-testid="plan-completed"
        className="rounded-card border border-teal/30 bg-shell p-6"
      >
        <h2 className="text-lg text-ink">
          已生成 {completed.route_count} 套方案
          {completed.cached ? "（命中缓存，未重新计算）" : ""}
        </h2>
        {completed.degraded_modes.length > 0 ? (
          <ul className="mt-2 space-y-1 text-xs leading-relaxed text-ink-soft">
            {completed.degraded_modes.map((mode) => (
              <li key={mode}>降级模式：{mode}</li>
            ))}
          </ul>
        ) : null}
        <div className="mt-3">
          <LlmSummary llm={completed.llm} />
        </div>
        <Link
          href={`/trip/${encodeURIComponent(completed.trip_id)}`}
          data-testid="plan-open-trip"
          className="mt-4 inline-flex min-h-11 items-center justify-center rounded-full bg-ink px-6 text-sm text-sand transition-transform duration-300 hover:scale-[1.03] motion-reduce:hover:scale-100"
        >
          查看这套方案
        </Link>
      </div>
    );
  }

  const currentStage = progress?.stage ?? 0;

  return (
    <div data-testid="plan-progress" className="rounded-card border border-line bg-shell p-6">
      <p className="tnum text-xs text-ink-faint">request_id：{requestId}</p>
      <h2 className="mt-2 text-lg text-ink">正在规划这条路线</h2>

      <ol className="mt-4 space-y-2">
        {STAGES.map((stage, index) => {
          const stageNo = index + 1;
          const done = currentStage > stageNo;
          const active = currentStage === stageNo;
          const detail = active && progress !== null ? describeDetail(progress.detail) : null;
          return (
            <li
              key={stage.key}
              data-testid={`plan-stage-${stage.key}`}
              data-state={done ? "done" : active ? "active" : "waiting"}
              className={`flex items-baseline gap-3 text-sm ${
                done || active ? "text-ink" : "text-ink-faint"
              }`}
            >
              <span className="tnum w-5 shrink-0 text-xs">{done ? "✓" : stageNo}</span>
              <span className="font-medium">
                {/* 活动阶段用后端给的文案（它更具体，比如"筛选广州 213 个地点"） */}
                {active && progress !== null ? progress.label : stage.label}
              </span>
              {active && progress !== null ? (
                <span className="tnum text-xs text-ink-faint">{progress.pct}%</span>
              ) : null}
              {detail === null ? null : (
                <span className="w-full text-xs leading-relaxed text-ink-soft">{detail}</span>
              )}
            </li>
          );
        })}
      </ol>

      <p className="mt-4 text-xs leading-relaxed text-ink-soft">
        {disconnected
          ? "进度流已断开（后端可能重启了）。可以刷新这一页重新连接；如果方案已经生成，刷新后就能直接看到。"
          : `已等待 ${elapsedS} 秒。`}
      </p>

      {disconnected ? null : elapsedS >= SLOW_S ? (
        <p data-testid="plan-slow-note" className="mt-1 text-xs leading-relaxed text-ink-soft">
          已经超过 {SLOW_S} 秒：可以刷新看看是否已经返回了快速方案，稍后再刷新通常会拿到更优的一版。
        </p>
      ) : elapsedS >= DEEP_CHECK_S ? (
        <p data-testid="plan-deep-check-note" className="mt-1 text-xs leading-relaxed text-ink-soft">
          校验比平时慢一些，正在深度校验候选路线。
        </p>
      ) : null}
    </div>
  );
}
