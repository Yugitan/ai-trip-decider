import { expect, type Page } from "@playwright/test";

/**
 * E2E 公共工具。
 *
 * ★ 为什么所有用例都提交**同一份默认表单** ★
 * 规划有"每 IP 每天 5 次冷规划"的限流（`config/limits.yaml`），而 `plan_cache`
 * 的复用**发生在限流之前** —— 所以同一份参数只有第一次是真冷规划，
 * 后面几次命中缓存、不消耗配额。整套 E2E 因此能稳稳跑完，
 * 而不是靠"把阈值调大"来绕过产品规则。
 *
 * 需要不同参数时（例如 E2E-03 的完整输入）显式改一个字段即可 —— 那一次是新的冷规划，
 * 所以这类用例数量要克制（当前只有 2 个）。
 */

/**
 * ⛔这里曾经有一份 `DEFAULT_PAYLOAD`（"默认表单交上去长什么样"）。已删除，因为：
 * 它没有任何地方用（真正在比对的是 `components/__tests__/planner-form-submit.test.tsx`
 * 里那份同名常量），而"没有任何地方用"意味着它不会跟着表单一起改 ——
 * `pace` 从全局控件改成按天的 `day_plans` 时，它就悄悄变成了一句假话。
 * 一份没人读的"现状记录"比没有更坏：它会让人以为默认值还是那样。
 */

/**
 * 第三方 SDK 自己的噪声 —— **逐条写明为什么不是我们的问题**。
 *
 * 只放一条：高德 JS SDK 的埋点请求（`/_AMapService/v3/log/init`）。
 * 实测：上游对这个端点返回 `application/octet-stream`，Chrome 拒绝执行脚本并打一条
 * `Refused to execute script ... MIME type` 的 error。它来自**第三方脚本自身**，
 * 与本项目的功能无关；我们的代理并没有篡改 Content-Type（见
 * `app/amap-proxy/[...path]/route.ts`：`Content-Type` 原样透传）。
 *
 * 白名单只有这一条，且必须写成模式而不是"忽略所有 error"：
 * 一旦出现别的 console error，这条断言必须红。
 */
const THIRD_PARTY_CONSOLE_NOISE: readonly RegExp[] = [/_AMapService\/v3\/log\/init/];

/**
 * 控制台与页面错误的收集器（E2E-14 用它）。
 *
 * 只收集 `error` 级别：Next.js 在 dev 下会打一堆无关的 `warning`
 * （HMR、React DevTools 提示），把它们算成"不干净"只会训练人忽略这个断言。
 */
export function trackPageErrors(page: Page): string[] {
  const problems: string[] = [];
  page.on("console", (message) => {
    const text = message.text();
    if (message.type() !== "error") return;
    if (THIRD_PARTY_CONSOLE_NOISE.some((pattern) => pattern.test(text))) return;
    problems.push(`console: ${text}`);
  });
  page.on("pageerror", (error) => problems.push(`pageerror: ${error.message}`));
  return problems;
}

/** 提交默认表单并等结果渲染出来；返回这次行程自己的地址（`/trip/{uuid}`）。 */
export async function planWithDefaults(page: Page): Promise<string> {
  await page.goto("/");
  await page.getByRole("button", { name: "开始规划" }).click();
  await expect(page.getByTestId("trip-result")).toBeVisible({ timeout: 60_000 });
  return currentTripPath(page);
}

/** 当前这一版行程的地址（从「在新页面打开」链接上读）。 */
export async function currentTripPath(page: Page): Promise<string> {
  const permalink = page.getByRole("link", { name: "在新页面打开" });
  await expect(permalink).toBeVisible();
  const href = await permalink.getAttribute("href");
  expect(href, "内嵌结果必须给出这一版自己的地址").toMatch(/^\/trip\/[0-9a-f-]{36}$/);
  return href as string;
}

/** 方案卡片（`trip-route-A/B/C`）。 */
export function routeCards(page: Page) {
  return page.locator("[data-testid^='trip-route-']");
}

/**
 * 选一个分段控件（`ui/segmented.tsx`：天数 / 每天玩多久 / 逐天节奏 / 预算口径）。
 *
 * `legend` 要写全：节奏现在是**按天**的一组分组，名字是「第 N 天节奏」——
 * 只写「节奏」会一个也匹配不上（多个分组同名时也无法区分是哪一天）。
 *
 * ★ 为什么不是 `getByRole("radio").check()` ★
 * radio 的真身是 `sr-only`（1px、被裁剪），上面盖着 `<label>`，Playwright 去点那个
 * **看不见的 input** 时会被 label 拦下（`intercepts pointer events`），偶尔还能过的唯一
 * 原因是默认值已经选中 —— `check()` 对已选中的元素是空操作，根本不点。换个值就必挂。
 * 用户真实做的是点那枚胶囊，所以这里点的是该分组里**可见**的那段文字。
 */
export async function chooseSegment(
  page: Page,
  legend: string,
  label: string,
): Promise<void> {
  await page
    .getByRole("group", { name: legend })
    .getByText(label, { exact: true })
    .click();
}
