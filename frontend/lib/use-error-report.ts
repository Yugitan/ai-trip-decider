"use client";

import { useEffect } from "react";

import { reportClientError } from "@/lib/api";

/**
 * 把一次渲染期错误上报给后端（PRD FR-13.5：`error_logs` 表）。
 *
 * ★ 为什么抽成一个 hook 而不是在两个 boundary 里各写一遍 ★
 * 两个 boundary（`app/error.tsx` 与 `app/global-error.tsx`）要做的是**同一件事**：
 * 上报一次，且只上报一次。各写一份的结果是可预见的 —— 一边加上了 `digest`、
 * 另一边忘了，而"忘了"这件事没有任何测试会发现（两条路径都得真的崩一次才知道）。
 *
 * `digest` 是 Next 给服务端错误的哈希，它同时是**我们唯一的关联键**：
 * 用户念出这个编号，我们才能在日志里找到那一次请求。所以它必须一起上报。
 *
 * 依赖数组是 `[component, error]`：同一个错误对象重复渲染不会重复上报，
 * 而换了组件/换了错误会再报一次 —— 那确实是两次不同的现场。
 */
export function useErrorReport(component: string, error: Error & { digest?: string }) {
  useEffect(() => {
    void reportClientError({
      component,
      // 空 message 时给一句实在话：空的错误信息在表里等于没写
      message: error.message || "浏览器没有提供错误信息",
      code: error.digest ?? null,
      context: { digest: error.digest ?? null },
    });
  }, [component, error]);
}
