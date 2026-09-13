/**
 * 知识库浏览页（`app/explore/[city]/page.tsx`）的渲染测试。
 *
 * 为什么能这样测：它是一个 **async 服务端组件** —— 本质上是一个
 * `async (props) => JSX` 的纯函数。直接 `await ExplorePage({...})` 拿到返回的
 * React 树再交给 RTL 渲染即可，不需要起 Next.js 运行时。
 *
 * 重点覆盖三类容易出错的输出：
 * 1. **诚实性标注**：营业时间未知 / 票价未知 / 估算值 / 评分来源 —— 少一个就等于对用户撒谎。
 * 2. **后端不可用**：必须给出明确的故障状态与修复指令，而不是空白页或假数据。
 * 3. **空结果与分页**：搜不到时不能看起来像"页面坏了"。
 */

import { render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import ExplorePage from "@/app/explore/[city]/page";
import {
  ApiError,
  NetworkError,
  getCityStats,
  listPlaces,
  listRoutes,
  type CityStats,
  type PlacePage,
  type RouteSummary,
} from "@/lib/api";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    getCityStats: vi.fn(),
    listPlaces: vi.fn(),
    listRoutes: vi.fn(),
  };
});

const getCityStatsMock = vi.mocked(getCityStats);
const listPlacesMock = vi.mocked(listPlaces);
const listRoutesMock = vi.mocked(listRoutes);

// ── 夹具 ────────────────────────────────────────────────────────────────────

const META = {
  request_id: "req-1",
  cached: false,
  cache_layer: null,
  degraded_modes: [],
  elapsed_ms: 4,
};

const STATS: CityStats = {
  city: "guangzhou",
  total_places: 1622,
  categories: [
    { category: "food", label: "餐饮", count: 400 },
    { category: "museum", label: "博物馆展览", count: 50 },
  ],
  verification: { verified: 86, probable: 1500 },
  opening_hours_known: 120,
  opening_hours_unknown: 1502,
};

function makePlace(overrides: Partial<PlacePage["items"][number]> = {}) {
  return {
    id: "p1",
    name: "广州塔",
    name_en: "Canton Tower",
    category: "photo",
    district: "海珠区",
    latitude: 23.1066,
    longitude: 113.3245,
    address: null,
    recommended_duration_min: 90,
    opening_hours_raw: null,
    price_min: null,
    price_max: null,
    indoor: null,
    best_time: ["sunset"],
    tags: ["拍照"],
    aliases: ["小蛮腰"],
    scores: { popularity: 0.87 },
    score_source: "curated" as const,
    verification_status: "verified",
    confidence: 0.9,
    unknown_fields: ["opening_hours", "price_min"],
    data_quality_flags: ["score_derived"],
    sources: [
      {
        name: "OpenStreetMap",
        url: "https://www.openstreetmap.org/node/1",
        source_type: "osm",
        credibility: 0.7,
        field_scope: ["name"],
        checked_at: null,
        http_status: 200,
      },
    ],
    ...overrides,
  };
}

const PLACES: PlacePage = {
  city: "guangzhou",
  filters: { q: null, category: null, district: null },
  page: { total: 1622, limit: 24, offset: 0, has_more: true },
  items: [makePlace()],
};

function makeRoute(overrides: Partial<RouteSummary> = {}): RouteSummary {
  return {
    id: "r1",
    slug: "guangzhou-classic-1",
    name: "经典一日",
    description: "把最有代表性的地标串成一条顺路的线。",
    route_type: "classic",
    archetype_hint: "relaxed",
    pace: "relaxed",
    difficulty: "easy",
    duration_min: 480,
    walking_distance_m: 3200,
    estimated_transport_time_min: 60,
    recommended_start_time: "09:00",
    recommended_end_time: "18:00",
    best_for: ["首次来广州"],
    stop_count: 3,
    stops: [
      {
        seq: 0,
        place_id: "p1",
        place_name: "广州塔",
        category: "photo",
        stay_min: 90,
        note: null,
        transport_to_next: "metro",
        transport_to_next_min: 20,
        distance_to_next_m: null,
      },
    ],
    metrics_are_estimated: true,
    estimated_budget: null,
    verification_status: "probable",
    source_name: "curated",
    ...overrides,
  };
}

const ROUTES = {
  city: "guangzhou",
  total: 44,
  items: [makeRoute()],
};

function mockHappyPath(overrides: { places?: PlacePage; routes?: typeof ROUTES } = {}) {
  getCityStatsMock.mockResolvedValue({ data: STATS, meta: META });
  listPlacesMock.mockResolvedValue({ data: overrides.places ?? PLACES, meta: META });
  listRoutesMock.mockResolvedValue({ data: overrides.routes ?? ROUTES, meta: META });
}

async function renderPage(
  searchParams: { q?: string; category?: string } = {},
  city = "guangzhou",
) {
  const ui = await ExplorePage({
    params: Promise.resolve({ city }),
    searchParams: Promise.resolve(searchParams),
  });
  return render(ui);
}

beforeEach(() => {
  getCityStatsMock.mockReset();
  listPlacesMock.mockReset();
  listRoutesMock.mockReset();
});

// ── 正常渲染 ────────────────────────────────────────────────────────────────

describe("知识库浏览页 · 正常渲染", () => {
  it("概况条显示真实规模，并把「营业时间已知」换算成百分比", async () => {
    mockHappyPath();
    await renderPage();

    // 限定在概况区里断言：地点总数「1622」在地点区标题里也会出现一次
    const stats = within(screen.getByRole("region", { name: "知识库概况" }));

    expect(screen.getByRole("heading", { name: "广州 知识库" })).toBeInTheDocument();
    expect(stats.getByText("1622")).toBeInTheDocument();      // 地点总数
    expect(stats.getByText("86")).toBeInTheDocument();        // 身份可交叉核验
    expect(stats.getByText("7%")).toBeInTheDocument();        // 120 / 1622 ≈ 7%
    expect(stats.getByText("1500")).toBeInTheDocument();      // 其余可信度
  });

  it("说明「怎么读这组数字」，而不是只丢一堆数", async () => {
    mockHappyPath();
    await renderPage();

    expect(screen.getByText(/怎么读这组数字/)).toBeInTheDocument();
    expect(screen.getByText(/我们不会用估算值填上/)).toBeInTheDocument();
  });

  it("地点卡展示停留时长、行政区、别名与热度分值来源", async () => {
    mockHappyPath();
    await renderPage();

    // 「广州塔」在页面里既是地点名也是路线站点名，必须限定在地点区里查
    const placesSection = screen.getByRole("heading", { name: /地点库/ }).closest("section");
    expect(placesSection).not.toBeNull();
    const card = within(placesSection as HTMLElement).getByText("广州塔").closest("li");
    expect(card).not.toBeNull();
    const scope = within(card as HTMLElement);

    expect(scope.getByText("90 分钟")).toBeInTheDocument();
    expect(scope.getByText("海珠区")).toBeInTheDocument();
    expect(scope.getByText(/又称：小蛮腰/)).toBeInTheDocument();
    expect(scope.getByText("0.87")).toBeInTheDocument();
    // 分值不是事实，必须能区分「人工校准」与「规则推导」
    expect(scope.getByTitle(/人工校准/)).toBeInTheDocument();
  });

  it("★ 未知字段必须标出来，而不是留空让人以为是「没有」", async () => {
    mockHappyPath();
    await renderPage();

    expect(screen.getByText("营业时间未知")).toBeInTheDocument();
    expect(screen.getByText("票价未知")).toBeInTheDocument();
  });

  it("★ 票价有值时必须显示出来（不能只报告「缺什么」而永远看不到「有什么」）", async () => {
    mockHappyPath({
      places: {
        ...PLACES,
        items: [
          makePlace({
            price_min: 20,
            price_max: 50,
            unknown_fields: ["opening_hours"],
          }),
          makePlace({ id: "p2", name: "免费公园", price_min: 0, price_max: 0, unknown_fields: [] }),
          // 数据矛盾（上下限反了）时不得用任何一端猜一个区间。
          // 营业时间给个已知值，这样卡里唯一的「未知」就是票价那行。
          makePlace({
            id: "p3",
            name: "数据异常点",
            opening_hours_raw: "全天",
            price_min: 80,
            price_max: 10,
            unknown_fields: [],
          }),
        ],
      },
    });
    await renderPage();

    // 「广州塔」在页面里既是地点名也是路线站点名 —— 必须限定在地点区里查
    const placesSection = within(
      screen.getByRole("heading", { name: /地点库/ }).closest("section") as HTMLElement,
    );
    const cardFor = (name: string) => {
      const card = placesSection.getByText(name).closest("li");
      expect(card).not.toBeNull();
      return within(card as HTMLElement);
    };

    expect(cardFor("广州塔").getByText("20–50 元")).toBeInTheDocument();
    expect(cardFor("免费公园").getByText("0 元")).toBeInTheDocument();
    expect(cardFor("数据异常点").getByText("未知")).toBeInTheDocument();
  });

  it("票价缺失时在地点卡里也是「未知」（与营业时间同等对待）", async () => {
    mockHappyPath();
    await renderPage();

    const placesSection = screen.getByRole("heading", { name: /地点库/ }).closest("section");
    const card = within(placesSection as HTMLElement).getByText("广州塔").closest("li");
    const scope = within(card as HTMLElement);

    // 卡片里同时有「营业时间 未知」与「票价 未知」两行
    expect(scope.getAllByText("未知")).toHaveLength(2);
  });

  it("★ 路线的时长与距离必须标注为估算值，预算必须标注未估算", async () => {
    mockHappyPath();
    await renderPage();

    expect(screen.getByText("时长与距离为估算值")).toBeInTheDocument();
    expect(screen.getByText("预算未估算")).toBeInTheDocument();
  });

  it("路线区显示模板总数，但只渲染前 6 条", async () => {
    mockHappyPath({
      routes: {
        city: "guangzhou",
        total: 44,
        items: Array.from({ length: 8 }, (_, i) =>
          makeRoute({ id: `r${i}`, slug: `route-${i}`, name: `路线 ${i}` }),
        ),
      },
    });
    await renderPage();

    expect(screen.getByText(/共 44 条，这里展示前 6 条/)).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "路线 0" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "路线 5" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "路线 6" })).toBeNull();
  });

  it("出行方式与方案原型被翻译成中文", async () => {
    mockHappyPath();
    await renderPage();

    expect(screen.getByText(/→ 地铁 20 分/)).toBeInTheDocument();
    expect(screen.getByText("轻松休闲")).toBeInTheDocument();
  });

  it("时长与距离按人话格式化（含 1 公里切换点）", async () => {
    mockHappyPath({
      routes: {
        city: "guangzhou",
        total: 2,
        items: [
          makeRoute({ id: "a", slug: "route-a", name: "八小时", duration_min: 480, walking_distance_m: 3200 }),
          makeRoute({ id: "b", slug: "route-b", name: "半小时", duration_min: 30, walking_distance_m: 800 }),
        ],
      },
    });
    await renderPage();

    expect(screen.getByText("8 小时")).toBeInTheDocument();
    expect(screen.getByText("3.2 km")).toBeInTheDocument();
    expect(screen.getByText("30 分钟")).toBeInTheDocument();
    expect(screen.getByText("800 m")).toBeInTheDocument();
  });

  it("列表按热度排序的说明与分页提示都在", async () => {
    mockHappyPath();
    await renderPage();

    expect(screen.getByText(/仅显示前 24 条（按热度排序）/)).toBeInTheDocument();
  });
});

// ── 来源链接的安全处理 ──────────────────────────────────────────────────────

describe("知识库浏览页 · 来源链接", () => {
  it("https 来源渲染成可点击外链，并带 noopener", async () => {
    mockHappyPath();
    await renderPage();

    const link = screen.getByRole("link", { name: /来源：OpenStreetMap/ });
    expect(link).toHaveAttribute("href", "https://www.openstreetmap.org/node/1");
    expect(link).toHaveAttribute("rel", "noreferrer noopener");
    expect(link).toHaveAttribute("target", "_blank");
  });

  it("★ 非 http(s) 协议降级为纯文本，但保留来源署名", async () => {
    mockHappyPath({
      places: {
        ...PLACES,
        items: [
          makePlace({
            sources: [
              {
                name: "可疑来源",
                url: "javascript:alert(1)",
                source_type: "web",
                credibility: 0.5,
                field_scope: ["name"],
                checked_at: null,
                http_status: null,
              },
            ],
          }),
        ],
      },
    });
    await renderPage();

    expect(screen.queryByRole("link", { name: /可疑来源/ })).toBeNull();
    // 来源可追溯是硬性要求：链接不可用不等于可以把出处抹掉
    expect(screen.getByText(/来源：可疑来源（链接不可用）/)).toBeInTheDocument();
  });
});

// ── 搜索与筛选 ──────────────────────────────────────────────────────────────

describe("知识库浏览页 · 搜索与筛选", () => {
  it("搜索词回显在标题与输入框里", async () => {
    mockHappyPath({
      places: { ...PLACES, filters: { q: "小蛮腰", category: null, district: null } },
    });
    await renderPage({ q: "小蛮腰" });

    expect(screen.getByText(/（搜索「小蛮腰」）/)).toBeInTheDocument();
    expect(screen.getByLabelText("搜索地点名称或别名")).toHaveValue("小蛮腰");
  });

  it("选中的类别用 aria-current 标出，且搜索词被带进 chip 链接", async () => {
    mockHappyPath({
      places: { ...PLACES, filters: { q: "塔", category: "museum", district: null } },
    });
    await renderPage({ q: "塔", category: "museum" });

    const activeChip = screen.getByRole("link", { name: /博物馆展览/ });
    expect(activeChip).toHaveAttribute("aria-current", "page");
    expect(activeChip).toHaveAttribute("href", "/explore/guangzhou?q=%E5%A1%94&category=museum");

    // 「全部」不再是当前页
    expect(screen.getByRole("link", { name: /全部/ })).not.toHaveAttribute("aria-current");
  });

  it("★ city 来自 URL 段，拼链接时必须编码（否则 a/b 会被拼成两个路径段）", async () => {
    mockHappyPath();
    await renderPage({}, "a/b");

    const allChip = screen.getByRole("link", { name: /全部/ });
    expect(allChip).toHaveAttribute("href", "/explore/a%2Fb");

    const form = document.querySelector("form[method=\"get\"]");
    expect(form).toHaveAttribute("action", "/explore/a%2Fb");
  });

  it("搜索表单是 GET 表单，带 q 与隐藏的 category", async () => {
    mockHappyPath();
    await renderPage({ category: "food" });

    const hidden = document.querySelector('input[type="hidden"][name="category"]');
    expect(hidden).toHaveAttribute("value", "food");
  });
});

// ── 空结果 ──────────────────────────────────────────────────────────────────

describe("知识库浏览页 · 空结果", () => {
  it("搜不到时给出可执行的下一步，而不是空白", async () => {
    mockHappyPath({
      places: {
        city: "guangzhou",
        filters: { q: "不存在", category: null, district: null },
        page: { total: 0, limit: 24, offset: 0, has_more: false },
        items: [],
      },
    });
    await renderPage({ q: "不存在" });

    expect(
      screen.getByText(/没有匹配的地点。换个关键词，或点上面的「全部」看看库里有什么。/),
    ).toBeInTheDocument();
    expect(screen.queryByText(/仅显示前 24 条/)).toBeNull();
  });
});

// ── 后端不可用 ──────────────────────────────────────────────────────────────

describe("知识库浏览页 · 后端不可用", () => {
  it("★ 网络不通：明确说「未连接到后端服务」并给出修复指令，绝不显示占位数据", async () => {
    getCityStatsMock.mockRejectedValue(new NetworkError(new Error("ECONNREFUSED")));
    listPlacesMock.mockRejectedValue(new NetworkError(new Error("ECONNREFUSED")));
    listRoutesMock.mockRejectedValue(new NetworkError(new Error("ECONNREFUSED")));

    await renderPage();

    expect(screen.getByText("未连接到后端服务")).toBeInTheDocument();
    expect(screen.getByText(/make dev/)).toBeInTheDocument();
    expect(screen.getByText(/这一页刻意不显示任何占位数据/)).toBeInTheDocument();
    expect(screen.queryByText("广州塔")).toBeNull();
  });

  it("★ 语义化 API 错误：显示后端的 message 与 hint", async () => {
    const error = new ApiError(
      {
        code: "UNSUPPORTED_CITY",
        message: "没有城市 shenzhen 的数据",
        hint: "目前只有广州（guangzhou）。",
        context: { slug: "shenzhen" },
      },
      422,
      "req-422",
    );
    getCityStatsMock.mockRejectedValue(error);
    listPlacesMock.mockRejectedValue(error);
    listRoutesMock.mockRejectedValue(error);

    await renderPage();

    expect(screen.getByText("读取知识库失败")).toBeInTheDocument();
    expect(screen.getByText("没有城市 shenzhen 的数据")).toBeInTheDocument();
    expect(screen.getByText("目前只有广州（guangzhou）。")).toBeInTheDocument();
  });

  it("三个请求任意一个失败都要落到故障页（Promise.all 不吞错）", async () => {
    getCityStatsMock.mockResolvedValue({ data: STATS, meta: META });
    listPlacesMock.mockResolvedValue({ data: PLACES, meta: META });
    listRoutesMock.mockRejectedValue(new NetworkError(new Error("ECONNREFUSED")));

    await renderPage();

    expect(screen.getByText("未连接到后端服务")).toBeInTheDocument();
  });
});
