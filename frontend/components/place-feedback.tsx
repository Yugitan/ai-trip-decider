"use client";

import { useState } from "react";

import { ApiError, submitFeedback, type FeedbackCategory } from "@/lib/api";

/**
 * 「报告错误」入口（PRD FR-11.5 AC-11.5）。
 *
 * 为什么它长在地点卡片上而不是另开一页：用户发现数据错的那一刻正是**看着那条数据**的时候。
 * 让他先跳到一个反馈表单、再想办法把"是哪个地点"说清楚，等于把成本转嫁给报告问题的人 ——
 * `place_id` 由卡片带过来，用户只需要说"哪里不对"。
 *
 * ★ 三条刻意的选择 ★
 * 1. **默认收起**：知识库浏览页的核心动作不是报错，一个常驻的表单会让人以为必须填点什么。
 * 2. **类别是必填、留言是可选**：一个勾选框就能定位问题（营业时间/票价/已关闭…）；
 *    强制留言会让人直接离开（后端也只要求类别）。
 * 3. **回执用后端的原话**：`note` 由服务端给出（"数据问题会在人工复核时核对来源后修正，
 *    不会自动覆盖知识库"）—— 前端不另写一句承诺，两处承诺迟早会有一处做不到。
 */

/** 与后端 `feedback` 表的 CHECK 约束同一份取值。 */
export const FEEDBACK_CATEGORY_LABELS: Readonly<Record<FeedbackCategory, string>> = {
  wrong_hours: "营业时间有误",
  wrong_price: "票价有误",
  closed: "已停业 / 关闭",
  bad_route: "路线不合理",
  wrong_coord: "位置不准",
  other: "其他问题",
};

const CATEGORIES = Object.keys(FEEDBACK_CATEGORY_LABELS) as FeedbackCategory[];

type State =
  | { kind: "idle" }
  | { kind: "sending" }
  | { kind: "sent"; note: string }
  | { kind: "failed"; message: string; hint: string };

export function PlaceFeedback({ placeId, placeName }: { placeId: string; placeName: string }) {
  const [open, setOpen] = useState(false);
  const [category, setCategory] = useState<FeedbackCategory>("wrong_hours");
  const [message, setMessage] = useState("");
  const [state, setState] = useState<State>({ kind: "idle" });

  async function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setState({ kind: "sending" });
    try {
      const result = await submitFeedback({
        category,
        message: message.trim() || null,
        place_id: placeId,
      });
      setState({ kind: "sent", note: result.data.note });
      setMessage("");
      setOpen(false);
    } catch (cause) {
      setState(
        cause instanceof ApiError
          ? { kind: "failed", message: cause.message, hint: cause.hint }
          : {
              kind: "failed",
              message: "没能把这条反馈送出去",
              hint: "确认后端已启动，或稍后再试。",
            },
      );
    }
  }

  return (
    <div className="w-full text-[11px]">
      {state.kind === "sent" ? (
        <p data-testid={`place-feedback-sent-${placeId}`} className="leading-relaxed text-teal-dark">
          已记录 · {state.note}
        </p>
      ) : null}

      {state.kind === "failed" ? (
        <p data-testid={`place-feedback-failed-${placeId}`} className="leading-relaxed text-coral">
          {state.message}（{state.hint}）
        </p>
      ) : null}

      {open ? (
        <form onSubmit={handleSubmit} className="mt-1 space-y-2">
          <label className="flex flex-col gap-1 text-ink-soft">
            <span>
              要报告「{placeName}」的哪一类问题
            </span>
            <select
              value={category}
              onChange={(event) => setCategory(event.target.value as FeedbackCategory)}
              data-testid={`place-feedback-category-${placeId}`}
              className="min-h-11 rounded-btn border border-line bg-sand px-2 text-xs text-ink"
            >
              {CATEGORIES.map((key) => (
                <option key={key} value={key}>
                  {FEEDBACK_CATEGORY_LABELS[key]}
                </option>
              ))}
            </select>
          </label>
          <label className="flex flex-col gap-1 text-ink-soft">
            <span>补充说明（可选，最多 1000 字）</span>
            <textarea
              value={message}
              onChange={(event) => setMessage(event.target.value)}
              maxLength={1000}
              rows={2}
              data-testid={`place-feedback-message-${placeId}`}
              className="rounded-btn border border-line bg-sand px-2 py-1.5 text-xs text-ink"
            />
          </label>
          <div className="flex flex-wrap items-center gap-2">
            <button
              type="submit"
              disabled={state.kind === "sending"}
              data-testid={`place-feedback-submit-${placeId}`}
              className="inline-flex min-h-11 items-center rounded-full bg-ink px-4 text-xs text-sand disabled:opacity-60"
            >
              {state.kind === "sending" ? "正在提交…" : "提交"}
            </button>
            <button
              type="button"
              onClick={() => setOpen(false)}
              className="inline-flex min-h-11 items-center rounded-full border border-line px-4 text-xs text-ink-soft"
            >
              取消
            </button>
          </div>
        </form>
      ) : (
        <button
          type="button"
          onClick={() => {
            setOpen(true);
            setState({ kind: "idle" });
          }}
          data-testid={`place-feedback-open-${placeId}`}
          className="inline-flex min-h-11 items-center text-ink-faint transition-colors duration-300 hover:text-coral"
        >
          报告错误
        </button>
      )}
    </div>
  );
}
