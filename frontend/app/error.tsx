"use client";

import Link from "next/link";

import { useErrorReport } from "@/lib/use-error-report";

/**
 * 路由级 error boundary（PRD FR-13.1 / FR-13.5）。
 *
 * ★ 为什么必须有一个 ★
 * 在此之前，任何一个渲染期异常都会让用户看到一个**空白页**：
 * React 在 App Router 里没有 error boundary 时直接把子树卸载掉，
 * 于是\"没有 loading、没有错误、没有重试\"——三态里只剩一态，而且是空的那种。
 *
 * ★ 两件事必须同时做 ★
 * 1. **给用户一句人话 + 一个可执行的下一步**（重试 / 回首页），
 *    这是 FR-13.1 的\"无裸白屏\"；
 * 2. **把这次错误记下来**（`POST /api/v1/errors` → `error_logs` 表，FR-13.5）。
 *    只做 1 的话，我们永远不知道用户撞上了什么；只做 2 的话，用户看不到任何东西。
 *
 * ★ 上报是\"尽力而为\"，绝不反过来伤害用户 ★
 * `reportClientError` 从不抛异常（见 `lib/api.ts` 的说明）——
 * 一个崩溃页里再抛出第二个异常，只会得到更少的现场信息。
 *
 * `digest` 是 Next 给服务端错误的哈希，界面上如实显示它：
 * 用户报障时把它念出来，比\"就那个页面报错了\"有用得多。
 */
export default function RouteError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useErrorReport("route-error-boundary", error);

  return (
    <div className="mx-auto flex min-h-dvh w-full max-w-2xl flex-col justify-center px-5 py-16 sm:px-8">
      <p className="text-xs tracking-[0.14em] text-ink-soft uppercase">页面出错了</p>
      <h1 className="mt-4 text-3xl leading-[1.1] tracking-[-0.01em] text-ink">
        这一页没能渲染出来
      </h1>
      <p className="mt-4 text-sm leading-relaxed text-ink-soft">
        问题出在我们这边，不是你的操作。已经自动记录了一次错误现场
        —— 你可以直接重试，也可以回到首页重新规划。
      </p>
      {error.digest === undefined ? null : (
        <p className="tnum mt-2 text-xs text-ink-faint">错误编号：{error.digest}</p>
      )}

      <div className="mt-6 flex flex-wrap items-center gap-3">
        <button
          type="button"
          onClick={reset}
          data-testid="error-retry"
          className="inline-flex min-h-11 items-center justify-center rounded-full bg-ink px-6 text-sm text-sand transition-transform duration-300 hover:scale-[1.03] motion-reduce:hover:scale-100"
        >
          重试
        </button>
        <Link
          href="/"
          className="inline-flex min-h-11 items-center justify-center rounded-full border border-line bg-shell px-6 text-sm text-ink-soft transition-colors duration-300 hover:border-teal/40 hover:text-ink"
        >
          回到首页
        </Link>
      </div>

      <p className="mt-6 text-xs leading-relaxed text-ink-faint">
        原始信息（排查用）：{error.message || "（浏览器没有提供更多信息）"}
      </p>
    </div>
  );
}
