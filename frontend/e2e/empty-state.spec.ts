import { expect, test } from "@playwright/test";

import { currentTripPath, planWithDefaults } from "./helpers";

/**
 * E2E-15 空状态（PRD §23.5 最后一条）。
 *
 * ★ 这一条比 PRD 的写法多测了两处，原因写在下面 ★
 * PRD 把 E2E-15 写成「清空 LocalStorage 访问 `/me` → 友好空状态 + CTA」。
 * 本仓库里 `/me` 已经按这个口径实现（`lib/recent-trips.ts` 的本地台账），
 * 但**"空状态要友好"这件事在这个产品里其实有三处**，而三处的共同要求只有一句：
 *   给一句真话 + 一条出路，不报错、不占位。
 * 只测 `/me` 会漏掉另两处 —— 而它们恰恰是用户更容易撞上的：
 *   ① `/me` 没有任何记录（PRD 字面上的那一处）；
 *   ② 分享链接失效/写错（`/t/{slug}`，别人手机里存了一条早就取消分享的链接）；
 *   ③ 行程 id 不存在或不属于当前会话（`/trip/{id}`，换了浏览器或清了站点数据）。
 *
 * 前两条不需要规划（不消耗冷规划配额），第三条也只是读一次；
 * 最后一条（15d）盘的是同一个功能的**另一半** —— 记录真的被写下来了并真能从清单里打开，
 * 它提交默认表单（命中 `plan_cache`，因此同样不消耗冷规划配额）。
 */

test.describe("空状态", () => {
  test("E2E-15a /me 没有记录：友好空状态 + CTA，不是报错", async ({ page }) => {
    // Playwright 每个用例默认一个全新上下文，localStorage 本来就是空的；
    // 仍然显式清一次 —— "这一条依赖存储为空"应当写在测试里，
    // 而不是依赖框架的默认行为（默认行为会变，测试不会因此变红）。
    await page.goto("/me");
    await page.evaluate(() => window.localStorage.clear());
    await page.reload();

    const empty = page.getByTestId("recent-trips-empty");
    await expect(empty).toBeVisible();
    await expect(empty.getByRole("heading", { name: "这里还是空的" })).toBeVisible();
    await expect(empty.getByRole("link", { name: "去规划一次" })).toHaveAttribute(
      "href",
      "/#planner",
    );

    // 「非报错」这一条要真的钉住：空状态里不该出现任何故障措辞
    await expect(page.getByText(/出错|失败了|服务不可用/)).toHaveCount(0);
    await expect(page.getByTestId("recent-trips-list")).toHaveCount(0);
  });

  test("E2E-15b 失效的分享链接：说清「链接没了」，仍然留一条出路", async ({ page }) => {
    await page.goto("/t/definitely-not-a-real-slug");

    const unavailable = page.getByTestId("share-unavailable");
    await expect(unavailable).toBeVisible();
    // 不占位：读不到就明说，而不是先给一份"看起来像真的"行程
    await expect(page.getByTestId("trip-result")).toHaveCount(0);
    await expect(page.getByText(/不显示任何占位行程/)).toBeVisible();
    // 出路：读不到别人的行程，也可以自己规划一次
    await expect(page.getByRole("link", { name: "我也要规划一次" })).toBeVisible();
  });

  test("E2E-15c 打不开的行程：人话错误 + 重试 + 回得去的入口", async ({ page }) => {
    // 这个 id 不是这个会话建的（后端按游客会话授权，别人的行程一律 404/403）
    await page.goto("/trip/00000000-0000-4000-8000-000000000000");

    const failure = page.getByTestId("trip-error");
    await expect(failure).toBeVisible({ timeout: 30_000 });
    await expect(failure.getByRole("button", { name: "重试" })).toBeVisible();
    // 不假装有内容
    await expect(page.getByTestId("trip-result")).toHaveCount(0);
    // 回得去的路
    await expect(page.getByRole("link", { name: "首页重新规划" })).toBeVisible();
    await expect(page.getByRole("link", { name: "我的行程" })).toBeVisible();
  });

  test("E2E-15d /me 记下了刚规划的这一版，并且能从这里把它再打开", async ({ page }) => {
    // 同一个上下文里先规划一次（默认表单 ⇒ 命中缓存 ⇒ 不消耗冷规划配额）
    const tripPath = await planWithDefaults(page);
    const tripId = (await currentTripPath(page)).replace("/trip/", "");

    await page.goto("/me");

    const record = page.getByTestId(`recent-trip-${tripId}`);
    await expect(record).toBeVisible();
    await expect(record.getByRole("link")).toHaveAttribute("href", tripPath);
    // 空状态不该同时出现（有一条记录时说"这里还是空的"就是假话）
    await expect(page.getByTestId("recent-trips-empty")).toHaveCount(0);

    // 从这里再打开它：行程照常渲染（记录不是死链）
    await record.getByRole("link").click();
    await expect(page).toHaveURL(new RegExp(`${tripPath}$`));
    await expect(page.getByTestId("trip-result")).toBeVisible();
  });
});
