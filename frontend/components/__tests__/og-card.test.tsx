import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { TripOut, TripRoute } from "@/lib/api";
import { OgCard, OgUnavailableCard } from "@/components/og-card";

/**
 * OG 卡片渲染层的 jsdom 断言：措辞与结构由这里守住，像素交给真实冒烟。
 * （satori 不在 jsdom 里跑 —— 这一层验证的是「画了什么」，不是「长什么样」。）
 */

function route(overrides: Partial<TripRoute> = {}): TripRoute {
  return {
    id: "route-A",
    label: "A",
    archetype: "relaxed",
    theme: null,
    name: "老城寻味慢行",
    one_liner: null,
    place_count: 2,
    total_duration_min: 317,
    walking_distance_m: 537,
    transit_time_min: 17,
    transit_distance_m: 2969,
    budget_min: "18.32",
    budget_max: "18.32",
    budget_scope: "per_person",
    budget_estimated: true,
    budget_unknown_items: [],
    recommend_score: 0.9,
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
      {
        seq: 1,
        place_id: "p2",
        name: "广州塔",
        category: "sight",
        latitude: 23.1066,
        longitude: 113.3245,
        district: null,
        arrive_time: "11:00",
        depart_time: "12:30",
        stay_min: 90,
        transport_mode: "metro",
        transport_min: 17,
        transport_distance_m: 2969,
        transport_source: "estimated",
        why_recommended: null,
        tips: null,
        warnings: [],
        snapshot: {},
      },
    ],
    ...overrides,
  };
}

function trip(overrides: Partial<TripOut> = {}): TripOut {
  return {
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
    routes: [route()],
    ...overrides,
  };
}

describe("OgCard", () => {
  it("画出行程标题、副标题、方案名与品牌", () => {
    render(<OgCard trip={trip()} />);

    expect(screen.getByText("广州 · 09:00–21:00")).toBeInTheDocument();
    expect(screen.getByText(/2 站 · 5 小时 17 分/)).toBeInTheDocument();
    expect(screen.getByText("老城寻味慢行")).toBeInTheDocument();
    expect(screen.getByText("TripDecider")).toBeInTheDocument();
  });

  it("站点示意图：每个站一个编号标记，最后一站是终点样式", () => {
    const { container } = render(<OgCard trip={trip()} />);

    const svg = container.querySelector("svg");
    expect(svg).not.toBeNull();
    // 2 个编号 + 虚线轨迹
    expect(svg?.querySelectorAll("circle").length).toBe(2);
    expect(svg?.querySelector("path")).not.toBeNull();
  });

  it("★ 没有路线时不画示意图，也不画编造的数字", () => {
    const { container } = render(<OgCard trip={trip({ routes: [] })} />);

    expect(container.querySelector("svg")).toBeNull();
    expect(screen.getByText("这份行程没有可展示的方案")).toBeInTheDocument();
    expect(screen.getByText("1 天 · 广州")).toBeInTheDocument();
  });

  it("单点行程也画标记（normalize 保证无 NaN），只是没有连线", () => {
    const single = route({
      place_count: 1,
      stops: [route().stops[0] ?? null].filter(Boolean) as TripRoute["stops"],
    });
    const { container } = render(<OgCard trip={trip({ routes: [single] })} />);

    expect(container.querySelectorAll("circle").length).toBe(1);
    expect(container.querySelector("path")).toBeNull();
  });
});

describe("OgUnavailableCard", () => {
  it("失效链接的空卡说的是真话，且没有任何行程内容", () => {
    render(<OgUnavailableCard message="这个分享链接已失效，或地址写错了。" />);

    expect(screen.getByText("这个分享链接不可用")).toBeInTheDocument();
    expect(screen.getByText("这个分享链接已失效，或地址写错了。")).toBeInTheDocument();
    expect(screen.queryByText("老城寻味慢行")).toBeNull();
    expect(document.querySelector("svg")).toBeNull();
  });
});
