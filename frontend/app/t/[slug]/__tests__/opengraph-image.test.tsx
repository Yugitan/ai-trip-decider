/**
 * `opengraph-image.tsx`（图片路由）的测试。
 *
 * ImageResponse 在 jsdom 里无法真正渲染（satori 需要 Node 原生环境），
 * 所以 mock 掉 `next/og`，断言的是**传给它的元素** —— 画的是行程卡还是空卡，
 * 以及路由导出的元数据（size / contentType / alt）是否正确。
 * 「真的能出一张 PNG」这件事由真实冒烟验证（RUNNING.md §8.10）。
 */

import { beforeEach, describe, expect, it, vi } from "vitest";

import Image, { alt, contentType, size } from "@/app/t/[slug]/opengraph-image";
import { getSharedTrip, type TripOut } from "@/lib/api";

let lastElement: React.ReactElement | null = null;

vi.mock("next/og", () => ({
  ImageResponse: class {
    constructor(element: React.ReactElement) {
      lastElement = element;
    }
  },
}));

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
  degraded_modes: [],
  total_cost_cny: "0",
  generation_ms: 200,
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
      one_liner: null,
      place_count: 1,
      total_duration_min: 317,
      walking_distance_m: 537,
      transit_time_min: 17,
      transit_distance_m: 2969,
      budget_min: "18.32",
      budget_max: "18.32",
      budget_scope: "per_person",
      budget_estimated: true,
      budget_unknown_items: [],
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
          name: "陈家祠",
          category: "sight",
          latitude: 23.1252,
          longitude: 113.2455,
          district: null,
          arrive_time: "09:00",
          depart_time: "10:30",
          stay_min: 90,
          transport_mode: null,
          transport_min: null,
          transport_distance_m: null,
          transport_source: null,
          why_recommended: null,
          tips: null,
          warnings: [],
          snapshot: {},
        },
      ],
    },
  ],
};

async function renderImage(slug = "abc123456789") {
  lastElement = null;
  await Image({ params: Promise.resolve({ slug }) });
  return lastElement;
}

beforeEach(() => {
  getSharedTripMock.mockReset();
});

describe("opengraph-image 路由", () => {
  it("导出的元数据符合社交平台通用规格", () => {
    expect(size).toEqual({ width: 1200, height: 630 });
    expect(contentType).toBe("image/png");
    expect(alt).toBe("分享的广州路线方案卡片");
  });

  it("读到行程时画行程卡", async () => {
    getSharedTripMock.mockResolvedValue({ data: TRIP, meta: { request_id: "r", cached: false, cache_layer: null, degraded_modes: [], elapsed_ms: 1 } });

    const element = await renderImage();

    const { render } = await import("@testing-library/react");
    render(element!);
    expect(document.querySelector("svg")).not.toBeNull();
    expect(document.body.textContent).toContain("老城寻味慢行");
  });

  it("★ 失效链接画「不可用」空卡，绝不画编造的行程", async () => {
    getSharedTripMock.mockRejectedValue(new Error("404"));

    const element = await renderImage("gone");

    const { render, screen } = await import("@testing-library/react");
    render(element!);
    expect(screen.getByText("这个分享链接不可用")).toBeInTheDocument();
    expect(screen.queryByText("老城寻味慢行")).toBeNull();
    expect(document.querySelector("svg")).toBeNull();
  });
});
