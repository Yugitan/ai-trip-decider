import { expect, test, type Page } from "@playwright/test";

import { planWithDefaults, routeCards } from "./helpers";

/**
 * 改路线 / 撤销 / 分享：E2E-07 · E2E-08 · E2E-09。
 *
 * 这三件事**必须按顺序**在同一个用例里做，因为它们操作的是同一份行程的版本链：
 * 撤销要撤销的是刚刚那次修改；分享要分享的是这一版。
 * 拆成独立用例就会各自重新规划一次，既多花配额，也测不出"版本链"这件事。
 */

test.describe.configure({ mode: "serial" });

/** 读当前这一版的地址（内嵌结果里的固定地址）。加载态时读不到，返回 null。 */
async function tripPath(page: Page): Promise<string | null> {
  const link = page.getByRole("link", { name: "在新页面打开" });
  if ((await link.count()) === 0) return null;
  return link.getAttribute("href");
}

/**
 * 等这次修改的**结局**，并明确告诉调用方是哪一种。
 *
 * ★ 为什么不能问"revision-note 现在可见吗" ★
 * 改成功之后组件会切到新版本并**重新取回行程**，中间有一段时间整块结果区
 * 处于 loading 早返回状态（两个提示都不在 DOM 里）。用 `isVisible()` 去问，
 * 恰好落在这个窗口里就会得到"既没改成也没反问"的错误结论 —— 第一版正是如此。
 * 所以这里轮询"地址变了没有"（改成功的确定性信号）与"有没有反问"。
 */
async function waitForRevisionOutcome(
  page: Page,
  before: string,
): Promise<"revised" | "clarify"> {
  const deadline = Date.now() + 30_000;
  while (Date.now() < deadline) {
    const now = await tripPath(page);
    if (now !== null && now !== before) return "revised";
    if ((await page.getByTestId("revision-clarify").count()) > 0) return "clarify";
    await page.waitForTimeout(300);
  }
  throw new Error("修改既没有产出新版本，也没有反问 —— 界面停在了中间态");
}

test("E2E-07/08 自然语言修改与撤销：Diff 说明可见、撤销回到上一版", async ({ page }) => {
  const before = await planWithDefaults(page);

  // ── E2E-07 修改 ───────────────────────────────────────────────────────
  await page.getByLabel("想改哪儿？用一句话说").fill("不要广州塔");
  await page.getByRole("button", { name: "改路线" }).click();

  const outcome = await waitForRevisionOutcome(page, before);

  if (outcome === "revised") {
    await expect(page.getByTestId("revision-note")).toContainText(/第 \d+ 版/);
    // 改完必须切到新版本（每次修改都是一条新 trip），并且新版本真的重新取了行程
    await expect(page.getByTestId("trip-result")).toBeVisible();

    // ── E2E-08 撤销 ────────────────────────────────────────────────────
    await page.getByRole("button", { name: "撤销这次修改" }).click();
    await expect(page.getByTestId("undo-note")).toContainText(/已回到第 \d+ 版/);
    await expect.poll(async () => await tripPath(page)).toBe(before);
  } else {
    // 规则引擎理解不了时会**反问**（AC-8.7）—— 这也算通过，但不许假装改好了
    await expect(page.getByTestId("revision-clarify")).toContainText(/没听明白/);
    expect(await tripPath(page), "没听懂时行程必须保持不动").toBe(before);
  }
});

test("E2E-09 分享：生成公开链接，无痕上下文免登录可看，且能复制这套路线", async ({
  page,
  browser,
}) => {
  await planWithDefaults(page);
  const routeCount = await routeCards(page).count();
  const firstName = (await page.getByTestId("trip-route-A").locator("h4").textContent()) ?? "";

  await page.getByRole("button", { name: "生成公开链接" }).click();
  // 分享要落库并提交，慢机器上可能超过默认 20s；这里给足预算而不是靠重试掩盖
  await expect(page.getByTestId("share-note")).toContainText(/链接已生成/, { timeout: 45_000 });

  const shareUrl = await page.locator("a[href*='/t/']").first().getAttribute("href");
  expect(shareUrl, "分享链接必须真的指向 /t/{slug}").toMatch(/\/t\/[0-9A-Za-z]{6,}$/);

  // 无痕上下文 = 另一个访客：分享页必须免登录可看（AC-9.2）
  const anonymous = await browser.newContext();
  try {
    const visitor = await anonymous.newPage();
    const response = await visitor.goto(shareUrl as string);
    expect(response?.status(), "分享页不该要求登录").toBe(200);

    await expect(visitor.getByTestId("trip-result")).toBeVisible();
    expect(await routeCards(visitor).count()).toBe(routeCount);
    // 同一个路线名会同时出现在卡片标题与对比表里 —— 这里只要求「至少出现一处」
    await expect(visitor.getByText(firstName).first()).toBeVisible();
    // 只读页面上唯一的动作：把这一套复制成自己的可编辑行程
    await expect(visitor.getByRole("button", { name: /复制这套路线/ })).toBeVisible();
  } finally {
    await anonymous.close();
  }
});
