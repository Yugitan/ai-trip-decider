/**
 * 公开分享页（`app/t/[slug]/page.tsx`）的测试。
 *
 * 为什么值得专门测：这一页是**别人**（没有这个浏览器会话的人）看路线的地方 ——
 * `POST /trips/{id}/share` 返回的就是它。如果它读不到数据、或者读不到时还画一份空壳，
 * "分享"这个功能对外就还是假的。
 *
 * 三条要守住的事：
 * 1. 读到行程时把内容渲染出来（路线名、站点、降级模式）；
 * 2. **读不到时明确说读不到**，不画占位行程（取消分享后链接会立刻失效，这是常态而非异常）；
 * 3. 页头有"我也要规划一次"的入口，且不提供任何能改这份行程的按钮（它是只读快照）。
 */

import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import SharedTripPage, { generateMetadata } from "@/app/t/[slug]/page";
import { ApiError, NetworkError, getSharedTrip, type TripOut } from "@/lib/api";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, getSharedTrip: vi.fn() };
});

const getSharedTripMock = vi.mocked(getSharedTrip);

const TRIP: TripOut = {
  trip_id: "trip-1",
  request_id: "req-1",
  city: "guangzhou",
  title: "广州 · 09:00–21:00",
  days: 1,
  revision_no: 1,
  route_count: 1,
  route_count_requested: 3,
  route_count_note: null,
  intent: {},
  degraded_modes: ["search:未配置搜索 API Key（只读本地知识库，不联网）"],
  total_cost_cny: "0",
  generation_ms: 225,
  created_at: null,
  is_public: true,
  share_slug: "abc123456789",
  routes: [
    {
      id: "route-1",
      label: "A",
      archetype: "relaxed",
      theme: null,
      name: "老城寻味慢行",
      one_liner: "3 站 · 约 5.3 小时 · 步行 0.5 km",
      place_count: 1,
      total_duration_min: 317,
      walking_distance_m: 537,
      transit_time_min: 17,
      transit_distance_m: 2969,
      budget_min: "18.32",
      budget_max: "18.32",
      budget_scope: "per_person",
      budget_estimated: true,
      budget_unknown_items: ["点都德·breakfast"],
      recommend_score: 0.97,
      feasibility: { feasible: true, violations: [], warnings: [], metrics: {} },
      best_for: [],
      highlights: [],
      pros: [],
      cons: [],
      recommendation_reason: null,
      route_source: "generated",
      template_route_id: null,
      stops: [
        {
          seq: 0,
          place_id: "p1",
          name: "点都德",
          category: "food",
          latitude: 23.12,
          longitude: 113.26,
          district: null,
          arrive_time: "09:00",
          depart_time: "10:30",
          stay_min: 90,
          transport_mode: "walk",
          transport_min: 8,
          transport_distance_m: 537,
          transport_source: "estimated",
          why_recommended: null,
          tips: null,
          warnings: ["ESTIMATED_TRANSIT"],
          snapshot: {},
        },
      ],
    },
  ],
};

const META = {
  request_id: "req-1",
  cached: false,
  cache_layer: null,
  degraded_modes: [],
  elapsed_ms: 12,
};

/** 服务端组件是 async 的：直接 await 它再渲染返回的元素。 */
async function renderPage(slug = "abc123456789") {
  const ui = await SharedTripPage({ params: Promise.resolve({ slug }) });
  render(ui);
}

beforeEach(() => {
  getSharedTripMock.mockReset();
});

describe("分享页", () => {
  it("渲染这份行程：路线、站点、预算与不确定性都说清楚", async () => {
    getSharedTripMock.mockResolvedValue({ data: TRIP, meta: META });

    await renderPage();

    expect(getSharedTripMock).toHaveBeenCalledWith("abc123456789");
    expect(screen.getByRole("heading", { name: "这份路线是别人规划好的" })).toBeInTheDocument();
    expect(screen.getByTestId("trip-result")).toBeInTheDocument();
    expect(screen.getByText("老城寻味慢行")).toBeInTheDocument();
    expect(screen.getByText(/09:00–10:30/)).toBeInTheDocument();
    // 估算值与未知项同样要露出来（分享页不比结果页宽松）
    expect(screen.getByText("（估算值）")).toBeInTheDocument();
    expect(screen.getByText(/未含价格：点都德·breakfast/)).toBeInTheDocument();
    expect(screen.getByText(/当前降级模式：search:/)).toBeInTheDocument();
    // 拉新入口
    expect(screen.getByRole("link", { name: "我也要规划一次" })).toHaveAttribute(
      "href",
      "/#planner",
    );
  });

  it("★ 只读：没有任何修改/撤销/分享入口", async () => {
    getSharedTripMock.mockResolvedValue({ data: TRIP, meta: META });

    await renderPage();

    expect(screen.queryByRole("button", { name: "改路线" })).toBeNull();
    expect(screen.queryByRole("button", { name: "撤销这次修改" })).toBeNull();
    expect(screen.queryByRole("button", { name: "生成公开链接" })).toBeNull();
  });

  it("★ 链接失效时明确说打不开，绝不画一份占位行程", async () => {
    getSharedTripMock.mockRejectedValue(
      new ApiError(
        {
          code: "SHARE_NOT_FOUND",
          message: "分享链接不存在或已失效",
          hint: "",
          context: {},
        },
        404,
        null,
      ),
    );

    await renderPage("gone");

    const block = screen.getByTestId("share-unavailable");
    expect(block).toHaveTextContent("这个分享链接打不开了");
    expect(block).toHaveTextContent("分享链接不存在或已失效");
    expect(block).toHaveTextContent(/读不到就说读不到/);
    expect(screen.queryByTestId("trip-result")).toBeNull();
  });

  it("连不上后端时也不画占位行程", async () => {
    getSharedTripMock.mockRejectedValue(new NetworkError(new Error("offline")));

    await renderPage();

    expect(screen.getByTestId("share-unavailable")).toHaveTextContent(
      "没能读到这份分享的行程",
    );
    expect(screen.queryByTestId("trip-result")).toBeNull();
  });

  it("标题带出行程名（分享预览要像样）", async () => {
    getSharedTripMock.mockResolvedValue({ data: TRIP, meta: META });

    const metadata = await generateMetadata({ params: Promise.resolve({ slug: "abc" }) });

    expect(metadata.title).toContain("广州 · 09:00–21:00");
  });

  it("读不到时也给出一个说得通的标题", async () => {
    getSharedTripMock.mockRejectedValue(new Error("boom"));

    const metadata = await generateMetadata({ params: Promise.resolve({ slug: "abc" }) });

    expect(metadata.title).toBe("这个分享链接不可用");
  });
});
