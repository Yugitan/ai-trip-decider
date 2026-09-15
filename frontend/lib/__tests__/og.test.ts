import { describe, expect, it } from "vitest";

import type { TripOut, TripRoute, TripStop } from "@/lib/api";
import {
  normalizeTrack,
  OG_SIZE,
  ogMetaLine,
  ogSubtitle,
  ogTitle,
  pickPrimaryRoute,
  tripJsonLd,
} from "@/lib/og";
import { siteUrl } from "@/lib/site";

/**
 * 分享页 OG 卡片与 JSON-LD 的纯逻辑层。
 *
 * 重点钉三类契约：
 * 1. **不知道就说不知道**：缺失的字段绝不冒出 0 / 空串 / 编造的说法；
 * 2. **选方案与页面上已有的口径一致**（评分最高），不引入新排名；
 * 3. **归一化不产生 NaN**（单点 / 重合点会让 satori 直接渲染失败）。
 */

function stop(overrides: Partial<TripStop> = {}): TripStop {
  return {
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
    ...overrides,
  };
}

function route(overrides: Partial<TripRoute> = {}): TripRoute {
  return {
    id: "route-A",
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
    recommend_score: 0.9,
    feasibility: { feasible: true, violations: [], warnings: [], metrics: {} },
    best_for: [],
    highlights: [],
    pros: [],
    cons: [],
    recommendation_reason: null,
    route_source: "generated",
    template_route_id: null,
    stops: [stop()],
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

describe("pickPrimaryRoute", () => {
  it("选 recommend_score 最高的那套（与页面排序口径一致）", () => {
    const t = trip({
      routes: [route({ id: "b", recommend_score: 0.7 }), route({ id: "a", recommend_score: 0.96 })],
    });
    expect(pickPrimaryRoute(t)?.id).toBe("a");
  });

  it("分数不可比较时按数据不足处理，不猜", () => {
    const t = trip({ routes: [route({ id: "x", recommend_score: Number.NaN })] });
    expect(pickPrimaryRoute(t)).toBeNull();
  });

  it("没有路线就是没有，不编一套出来", () => {
    expect(pickPrimaryRoute(trip({ routes: [] }))).toBeNull();
  });
});

describe("ogTitle", () => {
  it("优先用行程自己的标题", () => {
    expect(ogTitle(trip())).toBe("广州 · 09:00–21:00");
  });

  it("标题缺失/空白时退到「N 天 · 广州」，不再编更具体的说法", () => {
    expect(ogTitle(trip({ title: null }))).toBe("1 天 · 广州");
    expect(ogTitle(trip({ title: "   " }))).toBe("1 天 · 广州");
  });
});

describe("ogSubtitle", () => {
  it("数字齐全时是 站数 · 时长 · 预算", () => {
    expect(ogSubtitle(trip(), trip().routes[0] ?? route())).toBe("1 站 · 5 小时 17 分 · ¥18.32/人");
  });

  it("预算缺失时不冒出「¥」，只说拿得到的那一半", () => {
    const r = route({ budget_min: null, budget_max: null });
    expect(ogSubtitle(trip(), r)).toBe("1 站 · 5 小时 17 分");
  });

  it("时长为 0/负数/NaN 视为拿不到", () => {
    const r = route({ total_duration_min: 0, budget_min: null, budget_max: null });
    expect(ogSubtitle(trip(), r)).toBe("1 站");
    const bad = route({ total_duration_min: Number.NaN, budget_min: null, budget_max: null });
    expect(ogSubtitle(trip(), bad)).toBe("1 站");
  });

  it("数字一样都拿不到时退到方案名，而不是一句空话", () => {
    const r = route({
      place_count: 0,
      total_duration_min: Number.NaN,
      budget_min: null,
      budget_max: null,
      name: "老城寻味慢行",
    });
    expect(ogSubtitle(trip(), r)).toBe("老城寻味慢行");
  });

  it("没有任何路线时回落到「N 天 · 广州」", () => {
    expect(ogSubtitle(trip(), null)).toBe("1 天 · 广州");
  });
});

describe("ogMetaLine", () => {
  it("原型是中文，交通缺哪半说哪半未知", () => {
    expect(ogMetaLine(route())).toBe("轻松休闲 · 站间交通 17 分钟 · 3.0 km");
    const half = route({ transit_distance_m: null });
    expect(ogMetaLine(half)).toBe("轻松休闲 · 站间交通 17 分钟");
    const none = route({ transit_time_min: null, transit_distance_m: null });
    expect(ogMetaLine(none)).toBe("轻松休闲 · 站间交通 未知");
  });
});

describe("normalizeTrack", () => {
  it("两端点分别落在归一化包络的角上（留 pad）", () => {
    // 纬度跨度 0.4348 × cos(23°)≈0.92 ≈ 0.4，与经度跨度相等：
    // 两个方向都占满归一化空间，端点才会真正落到角上。
    const track = normalizeTrack([
      { latitude: 23.0, longitude: 113.0 },
      { latitude: 23.4348, longitude: 113.4 },
    ]);
    expect(track[0]!.x).toBeCloseTo(0.18, 3);
    expect(track[0]!.y).toBeCloseTo(0.82, 3);
    expect(track[1]!.x).toBeCloseTo(0.82, 3);
    expect(track[1]!.y).toBeCloseTo(0.18, 3);
  });

  it("经度占主导时纬向只占一格 —— 图形保持真实宽高比，不拉伸", () => {
    const track = normalizeTrack([
      { latitude: 23.0, longitude: 113.0 },
      { latitude: 23.2, longitude: 113.4 },
    ]);
    // 全部落在包络内（含 1 ulp 浮点噪声），x 占满、y 只占中间一段
    const EPS = 1e-9;
    for (const p of track) {
      expect(p.x).toBeGreaterThanOrEqual(0.18 - EPS);
      expect(p.x).toBeLessThanOrEqual(0.82 + EPS);
      expect(p.y).toBeGreaterThanOrEqual(0.18 - EPS);
      expect(p.y).toBeLessThanOrEqual(0.82 + EPS);
    }
    expect(track[1]!.x).toBeCloseTo(0.82, 10);
    expect(track[1]!.y).toBeLessThan(track[0]!.y);
  });

  it("★ 单点行程不产生 NaN（NaN 会让 satori 渲染直接失败）", () => {
    const track = normalizeTrack([{ latitude: 23.12, longitude: 113.26 }]);
    expect(track).toEqual([{ x: 0.5, y: 0.5 }]);
    expect(track[0]!.x).not.toBeNaN();
    expect(track[0]!.y).not.toBeNaN();
  });

  it("全部重合的点同样画在正中", () => {
    const track = normalizeTrack([
      { latitude: 23.12, longitude: 113.26 },
      { latitude: 23.12, longitude: 113.26 },
    ]);
    expect(track.every((p) => p.x === 0.5 && p.y === 0.5)).toBe(true);
  });

  it("非法坐标被剔除而不是画出去", () => {
    const track = normalizeTrack([
      { latitude: Number.NaN, longitude: 113.26 },
      { latitude: 23.0, longitude: Number.POSITIVE_INFINITY },
    ]);
    expect(track).toEqual([]);
  });

  it("南北向行程不会被压扁（纬度按 cos(lat) 修正）", () => {
    // 跨 0.2 纬度、0.02 经度：不修正时纬向 span 会吃掉经向 span。
    const track = normalizeTrack([
      { latitude: 23.0, longitude: 113.26 },
      { latitude: 23.2, longitude: 113.28 },
    ]);
    // 南北向行程里，北面的点 y 应明显小于南面的点（y 向下）。
    expect(track[1]!.y).toBeLessThan(track[0]!.y);
  });
});

describe("OG_SIZE", () => {
  it("是社交平台通用的 1200×630", () => {
    expect(OG_SIZE).toEqual({ width: 1200, height: 630 });
  });
});

describe("tripJsonLd", () => {
  it("产出 schema.org/TouristTrip，只写拿得到的字段", () => {
    const data = tripJsonLd(trip(), "https://t.example/t/abc123456789");
    expect(data["@context"]).toBe("https://schema.org");
    expect(data["@type"]).toBe("TouristTrip");
    expect(data.name).toBe("广州 · 09:00–21:00");
    expect(data.numDays).toBe(1);
    expect(data.url).toBe("https://t.example/t/abc123456789");
    expect(data.itinerary).toEqual(["1. 陈家祠（09:00–10:30）"]);
  });

  it("★ 时间缺失的站点只说站名，绝不写「（–）」这类占位", () => {
    const t = trip({
      routes: [route({ stops: [stop({ arrive_time: "", depart_time: "10:30" })] })],
    });
    const data = tripJsonLd(t, "https://t.example/t/x");
    expect(data.itinerary).toEqual(["1. 陈家祠"]);
  });

  it("没有路线时 itinerary 整个不出现（空数组会被当真数据）", () => {
    const data = tripJsonLd(trip({ routes: [] }), "https://t.example/t/x");
    expect("itinerary" in data).toBe(false);
  });

  it("超过 8 站只列前 8 个（机器摘要不需要全长清单）", () => {
    const stops = Array.from({ length: 11 }, (_, i) =>
      stop({ seq: i, place_id: `p${i}`, name: `站${i + 1}` }),
    );
    const data = tripJsonLd(trip({ routes: [route({ stops })] }), "");
    expect((data.itinerary as string[]).length).toBe(8);
    expect(data.itinerary).not.toContain("11. 站11");
  });

  it("numDays 为 0 时不写（0 天是编造）", () => {
    const data = tripJsonLd(trip({ days: 0 }), "");
    expect("numDays" in data).toBe(false);
  });

  it("pageUrl 为空串时 url 不写", () => {
    const data = tripJsonLd(trip(), "");
    expect("url" in data).toBe(false);
  });
});

describe("siteUrl", () => {
  it("读 SITE_URL 并去掉尾部斜杠", () => {
    const prev = process.env.SITE_URL;
    try {
      process.env.SITE_URL = "https://trip.example.com/";
      expect(siteUrl()).toBe("https://trip.example.com");
    } finally {
      if (prev === undefined) delete process.env.SITE_URL;
      else process.env.SITE_URL = prev;
    }
  });

  it("缺省落到 localhost:3000（本机预览可用）", () => {
    const prev = process.env.SITE_URL;
    try {
      delete process.env.SITE_URL;
      expect(siteUrl()).toBe("http://localhost:3000");
    } finally {
      if (prev !== undefined) process.env.SITE_URL = prev;
    }
  });
});
