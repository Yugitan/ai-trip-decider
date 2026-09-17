"use client";

import { useErrorReport } from "@/lib/use-error-report";

/**
 * 根布局级的 error boundary（PRD FR-13.5）。
 *
 * ★ 它与 `app/error.tsx` 的区别是**层级**，不是文案 ★
 * `error.tsx` 捕获的是页面子树里的异常，此时根布局（`<html>` / `<body>` / 字体）还在；
 * 而这个文件捕获的是**连根布局都失败**的情况 —— 所以它必须自己渲染
 * `<html>` 与 `<body>`，否则浏览器拿到的是一个没有骨架的文档。
 *
 * 这也是为什么它不引 Tailwind class 之外的任何组件（Header/Footer 都在布局里，
 * 而布局此刻可能正是炸掉的那一半），样式只用内联值兜底。
 */
export default function GlobalError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useErrorReport("global-error-boundary", error);

  return (
    <html lang="zh-CN">
      <body
        style={{
          margin: 0,
          padding: "3rem 1.25rem",
          fontFamily: "system-ui, -apple-system, 'PingFang SC', 'Microsoft YaHei', sans-serif",
          background: "#efe9dd",
          color: "#20242c",
        }}
      >
        <h1 style={{ fontSize: "1.5rem", margin: 0 }}>应用没能启动起来</h1>
        <p style={{ marginTop: "0.75rem", lineHeight: 1.7, color: "#4a5160" }}>
          整个页面都没能渲染，这通常是部署或配置问题。已经自动记录了一次错误现场。
        </p>
        {error.digest === undefined ? null : (
          <p style={{ marginTop: "0.5rem", fontSize: "0.75rem", color: "#8a93a8" }}>
            错误编号：{error.digest}
          </p>
        )}
        <button
          type="button"
          onClick={reset}
          data-testid="global-error-retry"
          style={{
            marginTop: "1.5rem",
            minHeight: "2.75rem",
            padding: "0 1.5rem",
            borderRadius: "9999px",
            border: "none",
            background: "#20242c",
            color: "#efe9dd",
            fontSize: "0.875rem",
            cursor: "pointer",
          }}
        >
          重试
        </button>
      </body>
    </html>
  );
}
