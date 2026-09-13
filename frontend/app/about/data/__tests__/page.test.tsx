/**
 * 「数据来源与免责」页（`app/about/data/page.tsx`）的渲染测试。
 *
 * 为什么值得专门测：这一页是纯文案，**没有测试就没人会发现它说得不对**。
 * 而它是产品对外的数据交代 —— 说错了就是另一种形式的伪造来源：
 *
 * 1. **必须署名 OpenStreetMap（ODbL）**：全部地点的坐标/标签/营业时间原文来自 OSM，
 *    这一页偏偏叫「数据从哪来」。署名写在浏览页页脚不算数，用户来这一页就是要看它。
 * 2. **不得写死规模数字**：曾写成「约 200+ 地点、30+ 精选路线」，
 *    而实际库里是 1622 个地点 —— 这类数字会随每次建库过时，只能指向可查的地方。
 * 3. **票价要说实话**：库里 1622 个地点的票价**全部为空**（没有可靠来源），
 *    所以这一页不能暗示我们有票价数据。
 */

import { render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import DataSourcesPage from "@/app/about/data/page";
import { NetworkError, getHealth } from "@/lib/api";

// 页面里带客户端组件（健康状态条），必须把网络出口换掉 —— 构建与测试都不允许真连后端。
vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    getHealth: vi.fn(),
  };
});

const getHealthMock = vi.mocked(getHealth);

beforeEach(() => {
  getHealthMock.mockReset();
  getHealthMock.mockRejectedValue(new NetworkError(new Error("offline")));
});

function section(name: string | RegExp): HTMLElement {
  return screen.getByRole("heading", { name }).closest("section") as HTMLElement;
}

/** 渲染页面并等健康状态条落定 —— 不等的话拒绝的 promise 会在用例结束后 setState，触发 act 警告。 */
async function renderPage(): Promise<void> {
  render(<DataSourcesPage />);
  await screen.findByText("未连接到后端服务");
}

describe("数据来源与免责页", () => {
  it("★ 必须署名 OpenStreetMap 与 ODbL 授权", async () => {
    await renderPage();

    const sources = section("数据从哪来");
    // OpenStreetMap 同时出现在卡片标题与正文里，所以查整段文本而不是单个元素
    const text = sources.textContent ?? "";

    expect(text).toMatch(/OpenStreetMap/);
    expect(text).toMatch(/ODbL/);
    expect(text).toMatch(/OpenStreetMap contributors/);
    // 署名要能追到具体条目，而不只是一句致谢
    expect(within(sources).getByText(/可点回原始 OSM 条目/)).toBeInTheDocument();
  });

  it("★ 不得写死知识库规模（会随每次建库过时）", async () => {
    await renderPage();

    const text = document.body.textContent ?? "";

    expect(text).not.toMatch(/200\+\s*地点/);
    expect(text).not.toMatch(/30\+\s*(精选)?路线/);
    // 不写死数字，就得告诉用户去哪里看真实数字
    expect(text).toMatch(/\/health|状态条|浏览页/);
  });

  it("★ 票价必须如实说是「没有来源」，不能暗示我们有票价数据", async () => {
    await renderPage();

    const text = document.body.textContent ?? "";

    expect(text).toMatch(/票价/);
    expect(text).toMatch(/没有可靠的票价来源|没有可靠来源/);
    // 「预算数字是估算区间」是旧文案：我们刻意不产出预算数字
    expect(text).not.toMatch(/预算数字是估算区间/);
  });

  it("三块来源说明都给出标题与正文，而不是只列关键词", async () => {
    await renderPage();

    const sources = section("数据从哪来");
    for (const heading of [
      /地点：OpenStreetMap/,
      /路线与编辑性评分：人工整理/,
      /排序与取舍：本地评分算法/,
      /票价：暂时没有/,
    ]) {
      expect(within(sources).getByRole("heading", { name: heading })).toBeInTheDocument();
    }
  });

  it("后端不可用时这一页仍然完整可用（它本身不依赖后端数据）", async () => {
    await renderPage();

    expect(screen.getByRole("heading", { name: "数据来源与免责声明" })).toBeInTheDocument();
    expect(screen.getByText("未连接到后端服务")).toBeInTheDocument();
  });

  it("明确写出「不写死数字」的原因，而不是默默省略", async () => {
    await renderPage();

    expect(screen.getByText(/刻意不写死数字/)).toBeInTheDocument();
  });
});
