import { expect, test, type Page } from "@playwright/test";

import { chooseSegment, planWithDefaults, routeCards, trackPageErrors } from "./helpers";

/**
 * 规划与结果页：E2E-02 提交 → E2E-03 完整输入 → E2E-04 至少 2 套方案
 * → E2E-05 地图 → E2E-06 方案对比 → E2E-14 console 清洁度。
 *
 * 串行执行：渲染 / 限流 / 缓存都是共享状态，并行只会制造无法复现的失败。
 * 除 E2E-03 外全部提交默认表单 —— 参数相同 ⇒ 命中 `plan_cache` ⇒ 不消耗冷规划配额
 * （见 helpers.ts 的说明）。
 */

test.describe.configure({ mode: "serial" });

test("E2E-02 默认值提交：内嵌结果出现，且这一版有自己的固定地址", async ({ page }) => {
  const tripPath = await planWithDefaults(page);

  await expect(page.getByTestId("trip-result")).toBeVisible();
  await expect(routeCards(page).first()).toBeVisible();

  // 「在新页面打开」必须真的指向同一版行程，打开后渲染的还是它
  await page.getByRole("link", { name: "在新页面打开" }).click();
  await page.waitForURL((url) => url.pathname === tripPath);
  await expect(page.getByTestId("trip-result")).toBeVisible();
  await expect(page.getByText(/套方案/).first()).toBeVisible();
});

test("E2E-03 完整输入提交：发出的 payload 是接口取值，不是界面中文", async ({ page }) => {
  await page.goto("/");

  await chooseSegment(page, "天数", "1 天");
  // 「玩几天」与「每天玩多久」是两个控件：只改其中一个时另一个保持默认，
  // 而两者都必须真的进 payload（否则界面上的选择会被静默丢弃）。
  await chooseSegment(page, "每天玩多久", "半天");
  await page.getByLabel("人数", { exact: true }).fill("2");
  // 偏好 chip 的真实 input 是 `sr-only`（视觉上是胶囊）—— 点 label 才是用户真正做的事，
  // 直接 .check() 会撞上"元素不可见/不稳定"（第一版就是这么挂的）
  await page.locator("label[for='planner-pref-美食']").click();
  await page.locator("label[for='planner-pref-拍照']").click();
  // 节奏与主题都是**按天**的（不再有全局「节奏」控件）。天数 = 1 时只有第 1 天一行。
  await chooseSegment(page, "第 1 天节奏", "轻松");
  // 主题下拉的选项文字带表情（"🍜 美食"），而 value 必须是枚举 key ——
  // 这就是这一条用例要盯的事，所以用 selectOption 的 **value** 而不是文字。
  await page.getByLabel("第 1 天主题").selectOption("food");
  await page.getByLabel("预算", { exact: true }).fill("300");
  await page.getByLabel("补充要求（可选）").fill("想吃早茶，走路别太多");

  await page.getByRole("button", { name: "开始规划" }).click();
  await expect(page.getByTestId("trip-result")).toBeVisible({ timeout: 60_000 });

  // ★ 这是真实踩过的坑 ★ 界面显示中文、接口只认枚举 key（发错就稳定 422）
  const payloadText = (await page.getByTestId("plan-payload").textContent()) ?? "";
  const payload = JSON.parse(payloadText) as Record<string, unknown>;
  expect(payload.city).toBe("guangzhou");
  expect(payload.preferences).toEqual(["food", "photo"]);
  // 不再发全局 `pace`：节奏随天走。这条断言同时钉住"长度 = days"（多一天就多一条，
  // 少了后端只会按兜底值排，界面上的选择被静默丢弃）。
  expect(payload.day_plans).toEqual([{ pace: "relaxed", theme: "food" }]);
  expect(payload.day_span).toBe("half_day");
  expect(payload.budget).toEqual({ amount: 300, scope: "per_person" });
  expect(payload.free_text).toBe("想吃早茶，走路别太多");

  // 响应里必须如实说明模型到底有没有被用到（配了 Key 与没配 Key 的不同都要能读出来）
  await expect(page.getByTestId("llm-summary")).toBeVisible();
});

test("E2E-04 至少 2 套方案：每套都有名称 / 时长 / 预算 / 地点数 / 站点明细", async ({
  page,
}) => {
  await planWithDefaults(page);

  const cards = routeCards(page);
  const count = await cards.count();
  expect(count, "PRD 要求至少 2 套合理方案").toBeGreaterThanOrEqual(2);

  for (let index = 0; index < count; index += 1) {
    const card = cards.nth(index);
    await expect(card.locator("h4")).not.toBeEmpty();
    await expect(card.getByText("总时长", { exact: true })).toBeVisible();
    // exact：卡片里还有一枚「预算含估算值」徽标，模糊匹配会撞上它
    await expect(card.getByText("预算", { exact: true })).toBeVisible();
    await expect(card.getByText(/^\d+ 个$/)).toBeVisible();
    // 站点明细必须有内容（空明细要么是后端明说，要么就不该出现在这里）
    expect(await card.locator("ol > li").count()).toBeGreaterThanOrEqual(3);
  }
});

test("E2E-05 地图：要么画出图，要么给一句真话 —— 站点列表永远完整", async ({ page }) => {
  await planWithDefaults(page);

  const mapContainer = page.getByTestId("route-map-A");
  const honestNote = page.getByText(
    /未配置地图 Key|画不出地图|地图初始化失败|地图加载中/,
  );

  const shown = (await mapContainer.count()) + (await honestNote.count());
  expect(shown, "地图既没画出来也没说明原因 —— 那就只是一个灰盒子").toBeGreaterThan(0);

  if ((await mapContainer.count()) > 0) {
    await expect(mapContainer).toBeVisible();
  }
  // 图注（地图真的画出来时）必须说明虚线只表示顺序，不是真实路线
  const caption = page.getByText(/先后顺序，不是实际行车路线/);
  if ((await caption.count()) > 0) {
    await expect(caption.first()).toBeVisible();
  }

  // 无论地图什么状态，站点列表都在 —— 地图坏了不能连带丢信息
  const card = page.getByTestId("trip-route-A");
  expect(await card.locator("ol > li").count()).toBeGreaterThanOrEqual(3);
});

/**
 * ★ E2E-06 的口径说明（PRD 与实现的差异，写在这里而不是偷偷改掉）★
 * PRD 的 E2E-06 写的是「点 Tab B → 时间线内容变化；地图折线更新；无整页重载」，
 * 那是「方案按 Tab 切换、一次只看一套」的交互。M5 的实现选择了另一条（见 TASKS.md M5-6/M5-7）：
 * **三套方案同时铺开 + 一张逐项对比表**（PRD §S8「方案对比表 3 列横排」）。
 * 两者解决的是同一个问题（"三套方案怎么比"），本条因此钉住的是**现在这个实现**里的
 * 同一件事：三套方案可并列比较、切换/对比不需要整页重载、表格里不许用破折号装成正常值。
 * 要不要改回 Tab，是产品决定；在它没变之前，断言必须描述真实存在的东西。
 */
test("E2E-06 方案对比：对比表覆盖全部方案，且不留占位符", async ({ page }) => {
  await planWithDefaults(page);

  const compare = page.getByTestId("route-compare");
  await expect(compare).toBeVisible();

  const routes = await routeCards(page).count();
  // 第一列是项目名，其余每套方案一列
  expect(await compare.locator("thead th").count()).toBe(routes + 1);
  // 对比项（名称/地点/总时长/步行/交通/预算/适合/需要留意/校验结果…）
  expect(await compare.locator("tbody tr").count()).toBeGreaterThanOrEqual(9);

  // 「不知道就说不知道」：表格里不许用破折号装成正常值（有单测钉住，这里再钉一次）
  await expect(compare.getByText("—", { exact: true })).toHaveCount(0);
});

test("E2E-14 console 清洁度：一次完整规划 + 一次修改交互不得产生 error 级日志", async ({
  page,
}) => {
  const problems = trackPageErrors(page);

  await planWithDefaults(page);
  await page.getByLabel("想改哪儿？用一句话说").fill("不要广州塔");
  await page.getByRole("button", { name: "改路线" }).click();
  // 改完或反问都行，只要不是报错 —— 关键是这一路不能往控制台吐 error
  await expect(
    page.getByTestId("revision-note").or(page.getByTestId("revision-clarify")),
  ).toBeVisible({ timeout: 30_000 });

  expect(problems, `控制台出现了 error：\n${problems.join("\n")}`).toEqual([]);
});

/** 供其它 spec 复用：从内嵌结果读出这一版的地址（`/trip/{uuid}`）。 */
export async function inlineTripPath(page: Page): Promise<string> {
  const href = await page.getByRole("link", { name: "在新页面打开" }).getAttribute("href");
  expect(href).toMatch(/^\/trip\/[0-9a-f-]{36}$/);
  return href as string;
}
