import { expect, test } from "@playwright/test";

/**
 * E2E-10 移动端布局（Pixel 7 / iPhone 14 两个项目跑同一份断言）：
 * 无横向滚动、CTA 触达目标 ≥44px、表单可以真的填。
 *
 * ★ 这一条刻意不提交规划 ★
 * 移动端与桌面走的是**同一套组件**（响应式只在 Tailwind 断点上不同），
 * 再提交一次规划只是重复消耗产品限流配额，多测不出任何东西。
 * "结果页在窄屏下的对比表要横向滑动"属于同一个组件的同一个断点，
 * 已由 `route-compare` 的单测与桌面 E2E-06 覆盖。
 */

test("E2E-10 移动端：首页无横向滚动，主 CTA 触达目标 ≥44px", async ({ page }) => {
  await page.goto("/");

  // ① 无横向滚动：溢出在手机上是最容易被忽略、也最影响可用性的缺陷
  const overflow = await page.evaluate(() => {
    const root = document.documentElement;
    return { scrollWidth: root.scrollWidth, clientWidth: root.clientWidth };
  });
  expect(
    overflow.scrollWidth,
    `页面横向溢出了：${overflow.scrollWidth} > ${overflow.clientWidth}`,
  ).toBeLessThanOrEqual(overflow.clientWidth + 1);

  // ② 触达目标：主 CTA 的高度不得小于 44px（iOS HIG / Material 的共同底线）
  const cta = page.getByRole("link", { name: "开始规划" });
  await expect(cta).toBeVisible();
  const box = await cta.boundingBox();
  expect(box, "主 CTA 没有尺寸").not.toBeNull();
  expect(box?.height ?? 0).toBeGreaterThanOrEqual(44);

  // ③ 表单在窄屏下可用：控件可聚焦、可输入，提交按钮同样 ≥44px
  const people = page.getByLabel("人数", { exact: true });
  await people.scrollIntoViewIfNeeded();
  await people.fill("3");
  await expect(people).toHaveValue("3");

  const submit = page.getByRole("button", { name: "开始规划" });
  await submit.scrollIntoViewIfNeeded();
  const submitBox = await submit.boundingBox();
  expect(submitBox?.height ?? 0).toBeGreaterThanOrEqual(44);
});
