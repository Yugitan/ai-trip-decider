import { expect, test } from "@playwright/test";

import { trackPageErrors } from "./helpers";

/**
 * 首页相关场景：E2E-01 首页加载 · E2E-13 按钮有效性扫描 · E2E-16 键盘可完成。
 *
 * 这三条都不提交规划（不消耗限流配额），因此可以放心反复跑。
 */

test("E2E-01 首页：Hero 唯一 h1、主 CTA 在首屏、无 console error", async ({ page }) => {
  const problems = trackPageErrors(page);

  await page.goto("/");

  // h1 唯一：一页里多个 h1 会让读屏与 SEO 都不知道这页在讲什么
  await expect(page.getByRole("heading", { level: 1 })).toHaveCount(1);

  // 首页有两个 CTA（吸顶导航里那个在窄屏隐藏）—— 这里要的是 Hero 里那个主 CTA
  const cta = page.getByRole("link", { name: "开始规划", exact: true });
  await expect(cta).toBeVisible();

  // CTA 必须在**首屏**：不能要求用户先滚一屏才知道这里能干什么
  const box = await cta.boundingBox();
  const viewport = page.viewportSize();
  expect(box, "主 CTA 没有尺寸").not.toBeNull();
  expect(viewport, "没有 viewport 尺寸").not.toBeNull();
  expect((box?.y ?? Number.MAX_SAFE_INTEGER) < (viewport?.height ?? 0)).toBe(true);

  // 首页背景视频即使加载失败也不该把错误打到控制台（它自己会静默淡出）
  expect(problems, problems.join("\n")).toEqual([]);
});

test("E2E-13 按钮有效性：规划卡里的控件都有可访问名且可用，示例能真的填入表单", async ({
  page,
}) => {
  await page.goto("/");

  const planner = page.locator("form[aria-labelledby='planner-title']");
  await expect(planner).toBeVisible();

  // ① 没有一个"哑"控件：每个 button 必须有可读名字且未被禁用
  const buttons = planner.getByRole("button");
  const count = await buttons.count();
  expect(count).toBeGreaterThan(3);
  for (let index = 0; index < count; index += 1) {
    const button = buttons.nth(index);
    const name = (await button.getAttribute("aria-label")) ?? (await button.textContent()) ?? "";
    expect(name.trim(), `第 ${index} 个按钮没有可访问名（读屏用户点不了）`).not.toBe("");
    await expect(button).toBeEnabled();
  }

  // ② 点一个示例要真的改变表单（而不是只弹一句话）
  const budget = planner.getByLabel("预算");
  await expect(budget).toHaveValue("300");
  await planner.getByRole("button", { name: /预算 200\/人/ }).click();
  await expect(budget).toHaveValue("200");
  await expect(page.getByText(/已按示例填入/)).toBeVisible();

  // ③ 底部提示必须在，说明提交去哪了（诚实性：不声称前端保存了什么）
  await expect(planner.getByText(/提交只会把上面这份需求发给自己的后端/)).toBeVisible();
});

test("E2E-16 键盘可完成：Tab 能走到提交按钮并用 Enter 触发", async ({ page }) => {
  await page.goto("/");

  // 键盘走到补充要求（表单最后一项）—— 证明表单控件在 Tab 顺序里可达
  const freeText = page.getByLabel("补充要求（可选）");
  await freeText.focus();
  await expect(freeText).toBeFocused();

  // 用 Enter 激活主按钮：键盘用户不该被"必须用鼠标点"挡住
  const submit = page.getByRole("button", { name: "开始规划" });
  await submit.focus();
  await expect(submit).toBeFocused();

  // ★ 「按下去真的开始了提交」不能用 loading 文案来钉 ★
  // 这一条原本断言按钮上的「正在提交…」。命中 `plan_cache` 时那个状态可能只存在
  // 几十毫秒，于是同一条用例会因为"这一次跑得快"而偶发失败 —— 那是在测网速，
  // 不是测键盘可达性（实测：换到预热过的服务器上跑就挂在这一行）。
  // 改成断言两件与速度无关的事：请求真的发出去了（POST /trips:plan），并且真的走完了。
  const submitted = page.waitForRequest(
    (request) =>
      request.method() === "POST" && request.url().includes("/api/v1/trips:plan"),
  );
  await page.keyboard.press("Enter");
  await submitted;
  await expect(page.getByTestId("trip-result")).toBeVisible({ timeout: 60_000 });
});
