import path from "node:path";
import { fileURLToPath } from "node:url";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

const rootDir = path.dirname(fileURLToPath(import.meta.url));

export default defineConfig({
  plugins: [react()],
  resolve: {
    // 与 tsconfig 的 `@/*` → `frontend/*` 保持一致
    alias: { "@": rootDir },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./vitest.setup.ts"],
    include: ["components/**/*.test.{ts,tsx}", "lib/**/*.test.ts", "app/**/*.test.{ts,tsx}"],
    // 组件不依赖真实样式，跳过 CSS 处理可以让测试更快、更稳
    css: false,
    restoreMocks: true,
    clearMocks: true,
    coverage: {
      // provider 与 vitest 同大版本（3.2.4）：v8 与 5.x 对不上会直接报 MISSING DEPENDENCY。
      provider: "v8",
      reporter: ["text", "text-summary"],
      // 只统计**我们写的逻辑**：lib 是纯逻辑，components 是交互组件，app 是页面。
      // app 必须一起计入 —— 页面里有真实的判断逻辑（空结果、故障分支、诚实性标注），
      // 漏掉它会让"覆盖率"看着很高却没有覆盖最难测的那部分。
      // 排除测试文件自身与纯样式化的 ui/ 原语（它们的正确性由使用方覆盖）。
      include: ["lib/**/*.ts", "components/**/*.tsx", "app/**/*.tsx"],
      exclude: ["**/__tests__/**", "**/*.test.*", "components/ui/**"],
      // 与后端 `COV_MIN=93`（真实约 95%）同一个思路：留一点缓冲，
      // 让它能挡住"悄悄拉低覆盖率"的改动，又不会因为无关小改动频繁失败。
      //
      // 实测（2026-09-12）：statements 98.56 / branches 85.80 / functions 91.66。
      // 分支阈值明显低一截是**如实反映现状**：组件里的 JSX 分支（加载中 / 降级 /
      // 空结果）很难穷尽，与其把阈值压到能过，不如把它写在明面上。
      thresholds: {
        statements: 96,
        branches: 84,
        functions: 89,
        lines: 96,
      },
    },
  },
});
