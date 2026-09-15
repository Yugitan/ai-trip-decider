import { defineConfig, devices } from "@playwright/test";

/**
 * Playwright 配置（PRD §23.4：桌面 chromium + Pixel 7 + iPhone 14）。
 *
 * ★ 三条刻意的选择 ★
 *
 * 1. **Worker 数为 1、串行执行**：整套 E2E 跑在**真实数据库**上，而且规划有
 *    "每 IP 每天 5 次冷规划"的产品限流。并发跑只会让多个用例互相抢配额，
 *    得到的 429 与并发能力无关。20 并发的吞吐验证属于压测（`scripts/benchmark.py`）。
 * 2. **`reuseExistingServer: true`**：本地 `make dev` 已经起着前后端时，
 *    Playwright 不该再起一份（端口冲突比测试失败更难排查）。
 *    需要它自动拉起时把 `make dev` 停掉即可。
 * 3. **mobile.spec.ts 单独匹配**：布局用例只在移动项目里跑，
 *    桌面项目里跑一遍移动布局没有意义，只会让整套慢三倍。
 *
 * 前置条件（`make e2e` 会检查）：
 *   - Postgres 在跑且**开发库已建库**（`make seed`），否则"至少 2 套方案"根本排不出来；
 *   - `frontend/.env.local` 可留空：浏览器只跟 `localhost:3000` 同源说话，
 *     `/api/*` 由 next.config.ts 的 rewrites 转发给后端。
 */
const BACKEND_URL = process.env.E2E_BACKEND_URL ?? "http://127.0.0.1:8000";
const FRONTEND_URL = process.env.E2E_FRONTEND_URL ?? "http://localhost:3000";

export default defineConfig({
  testDir: "./e2e",
  // 用 Playwright 的默认目录名：`.gitignore` 已经忽略 `test-results/`，
  // 失败时的 trace 与截图不会被误提交
  outputDir: "../test-results",
  // 一次冷规划要几秒，加上首屏编译（next dev）与地图脚本，90 秒是够且不虚的预算
  timeout: 90_000,
  expect: { timeout: 20_000 },
  fullyParallel: false,
  workers: 1,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 1 : 0,
  reporter: [["list"]],
  use: {
    baseURL: FRONTEND_URL,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "off",
  },
  projects: [
    {
      name: "chromium-desktop",
      use: { ...devices["Desktop Chrome"] },
      testIgnore: /mobile\.spec\.ts/,
    },
    { name: "pixel-7", use: { ...devices["Pixel 7"] }, testMatch: /mobile\.spec\.ts/ },
    {
      name: "iphone-14",
      /**
       * iPhone 14 的**视口与 UA 模拟**，但引擎用 chromium。
       *
       * Playwright 给 iPhone 系列的默认引擎是 WebKit（`webkit-2336`），
       * 而本机只缓存了 chromium —— 要用 WebKit 得额外下载约 90MB 的浏览器二进制。
       * 本项目的移动用例断言的是**布局**（无横向溢出、触达目标 ≥44px），
       * 这些与引擎无关；真要验证 iOS Safari 的独有行为（如 `100dvh`、弹性滚动）
       * 必须先装上 WebKit，并在文档里明说“截稿时这一档未验证”。
       */
      use: { ...devices["iPhone 14"], defaultBrowserType: "chromium" },
      testMatch: /mobile\.spec\.ts/,
    },
  ],
  webServer: [
    {
      // 用 --reload 关掉的生产式启动：E2E 要的是"一个稳定的后端"，热重载会中途重启
      command: "uv run uvicorn app.main:app --host 127.0.0.1 --port 8000",
      cwd: "../backend",
      url: `${BACKEND_URL}/api/v1/health`,
      reuseExistingServer: true,
      timeout: 120_000,
    },
    {
      command: "pnpm dev",
      url: FRONTEND_URL,
      reuseExistingServer: true,
      timeout: 180_000,
      env: { API_BASE_URL: BACKEND_URL },
    },
  ],
});
