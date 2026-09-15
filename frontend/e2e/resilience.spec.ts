import { expect, test } from "@playwright/test";

import { planWithDefaults } from "./helpers";

/**
 * E2E-11 错误恢复：后端 500 时必须给出**人话错误**，并且重试能成功。
 *
 * 这里用 Playwright 的 route mock 造一个 500 —— 与后端 `FAULT_INJECTION=db_down`
 * （见 `backend/tests/integration/test_fault_injection.py`）是两种互补的做法：
 * 那一边测"后端自己坏掉时返回什么"，这一边测"前端拿到那份错误后给用户看什么"。
 */

test("E2E-11 后端 500：显示人话错误与 request_id，重试后成功拿到方案", async ({ page }) => {
  await page.route("**/api/v1/trips:plan*", async (route) => {
    await route.fulfill({
      status: 500,
      contentType: "application/json",
      body: JSON.stringify({
        ok: false,
        error: {
          code: "INTERNAL",
          message: "服务出了点问题，请重试。",
          hint: "若持续出现，请把响应头里的 X-Request-Id 反馈给我们。",
          context: {},
        },
        meta: { request_id: "e2e-fake-request-id" },
      }),
    });
  });

  await page.goto("/");
  await page.getByRole("button", { name: "开始规划" }).click();

  // ① 人话错误：标题 + 后端原话 + 提示，三样都要有
  await expect(page.getByText("后端没有接受这次规划请求")).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText("服务出了点问题，请重试。").first()).toBeVisible();
  await expect(page.getByText(/把响应头里的 X-Request-Id 反馈给我们/)).toBeVisible();

  // ② 把"到底发了什么"留在页面上，便于对照（而不是一个转圈或空白）
  await expect(page.getByTestId("plan-payload")).toBeVisible();
  await expect(page.getByText(/HTTP 500/)).toBeVisible();
  await expect(page.getByText(/INTERNAL/)).toBeVisible();

  // ③ 不能假装成功了：此刻不该出现任何方案卡片
  await expect(page.getByTestId("trip-result")).toHaveCount(0);

  // ④ 重试（去掉 mock 再点一次）必须成功 —— 否则用户就被卡死在这一屏
  await page.unroute("**/api/v1/trips:plan*");
  await page.getByRole("button", { name: "开始规划" }).click();
  await expect(page.getByTestId("trip-result")).toBeVisible({ timeout: 60_000 });
  await expect(page.locator("[data-testid^='trip-route-']").first()).toBeVisible();
});

/**
 * E2E-12 地图服务失败降级：**不白屏、不丢信息、不装样子**。
 *
 * 地图是本项目唯一一个"不归我们管的"渲染面（高德 SDK 在第三方域名上，浏览器扩展、
 * 运营商、上游故障都能让它挂掉）。它挂掉时的正确表现有三条，缺一条都算降级失败：
 * ① 页面主体照常可用（不是一片白）；② 文字版路线顺序完整（地图坏不能连带丢信息）；
 * ③ 地图那一格给一句**真话**，而不是留一个空灰盒子。
 *
 * ★ 为什么用 abort 而不是 404 ★
 * SDK 被拦与网络层失败会走同一条代码路径（`lib/amap.ts` 的 `script.onerror`），
 * 那正是我们要钉住的降级分支；`fulfill(404)` 反而可能拿到一个"成功加载的"错误页。
 *
 * ★ 两种环境都要成立 ★
 * 配了 `NEXT_PUBLIC_AMAP_JS_KEY` 时这里测的是"脚本加载失败"，没配时测的是
 * "未配置 Key"。两条都是真话，都算降级成功 —— 所以先看这次到底有没有去要脚本，
 * 再断言对应的那一句，而不是把环境差异写死成某一句。
 */
test("E2E-12 地图服务失败降级：地图说真话，站序照常可读，不白屏", async ({ page }) => {
  let sdkRequested = false;
  await page.route("https://webapi.amap.com/**", async (route) => {
    sdkRequested = true;
    await route.abort();
  });

  await planWithDefaults(page);

  // ① 不白屏：结果面板与方案卡片都在
  await expect(page.getByTestId("trip-result")).toBeVisible();
  const cardA = page.getByTestId("trip-route-A");
  await expect(cardA).toBeVisible();

  // ② 文字版路线顺序完整
  expect(await cardA.locator("ol > li").count()).toBeGreaterThanOrEqual(3);

  // ③ 一句真话
  if (sdkRequested) {
    await expect(page.getByText(/地图脚本加载失败/).first()).toBeVisible();
    await expect(page.getByText(/站点列表不受影响/).first()).toBeVisible();
    // 画不出来时不能留一个空白容器在那里冒充地图
    await expect(page.getByTestId("route-map-A")).toHaveCount(0);
  } else {
    await expect(page.getByText(/未配置地图 Key/).first()).toBeVisible();
  }
});
