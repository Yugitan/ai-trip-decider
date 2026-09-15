/**
 * 分享页 OG 卡片与 JSON-LD 的**纯逻辑层**。
 *
 * 为什么把逻辑从 `opengraph-image.tsx` / 页面里抽出来：那两个文件一个跑在
 * 图片路由（不好断言），一个是 async 服务端组件（要 mock 整条请求链）；
 * 而「从一份 `TripOut` 里挑哪套方案、怎么措辞、坐标怎么归一化」恰恰是
 * 最该被单测钉住的部分 —— 尤其「不知道就说不知道」这类契约（同 `lib/format.ts`）。
 *
 * 与渲染层（`trip-result.tsx`）共用同一批格式化函数：**同一个数字不能在
 * 卡片和分享预览里长成两个样子。**
 */

import type { TripOut, TripRoute } from "./api";
import {
  formatArchetype,
  formatBudget,
  formatDuration,
  formatTransit,
} from "./format";
import { SITE_NAME } from "./site";

/** OG 卡片的尺寸（Facebook / 微信 / Twitter summary_large_image 的通用规格）。 */
export const OG_SIZE = { width: 1200, height: 630 } as const;

/**
 * 选出画在卡片上的那套方案：**优先 recommend_score 最高**。
 *
 * 为什么是分数：这是页面上已有的口径（卡片列表按它排序），分享预览沿用
 * 「这套是三选一里评分最高的」这个既成事实，不引入新的排名逻辑；
 * 也不把分数本身画上卡片 —— 一张图解释不了 7 维权重 + 乘数（同对比表纪律 1）。
 * 没有路线时返回 `null`，由调用方画一张不撒谎的空卡（绝不是占位行程）。
 */
export function pickPrimaryRoute(trip: TripOut): TripRoute | null {
  if (trip.routes.length === 0) return null;
  const scored = trip.routes.filter((r) => Number.isFinite(r.recommend_score));
  if (scored.length === 0) return null;
  return scored.reduce((best, r) => (r.recommend_score > best.recommend_score ? r : best));
}

/** 卡片主标题：行程自己的标题优先，退化到「N 天 · 广州」（不再编更具体的说法）。 */
export function ogTitle(trip: TripOut): string {
  if (trip.title && trip.title.trim() !== "") return trip.title.trim();
  return `${trip.days} 天 · 广州`;
}

/** 站点数措辞：0 站就不说站（空行程写成「3 站」是撒谎）。 */
function stopWord(count: number): string {
  return count > 0 ? `${count} 站` : "";
}

/**
 * 卡片副标题。**每个数字只说拿得到的那一半**（与 `formatTransit` 的纪律一致）：
 * 拿不到时长的卡片不会冒出「时长」，拿不到预算的卡片不会冒出「¥」。
 * 至少回落到「N 天 · 广州」—— 一句真实存在的信息。
 */
export function ogSubtitle(trip: TripOut, route: TripRoute | null): string {
  const parts: string[] = [];
  if (route !== null) {
    const stops = stopWord(route.place_count);
    if (stops !== "") parts.push(stops);
    const duration = formatDuration(
      Number.isFinite(route.total_duration_min) ? route.total_duration_min : null,
    );
    if (duration !== "时长未知") parts.push(duration);
    const budget = formatBudget(route.budget_min, route.budget_max, route.budget_scope);
    if (budget !== "未知") parts.push(budget);
    if (parts.length === 0 && route.name.trim() !== "") {
      // 数字一样都拿不到时，方案名本身就是真实的信息。
      parts.push(route.name.trim());
    }
  }
  if (parts.length === 0) parts.push(`${trip.days} 天 · 广州`);
  return parts.join(" · ");
}

/**
 * 卡片左下角的标签行：原型（中文）与站间交通合计。
 * 交通用 `formatTransit`，未知部分照实写「未知」，**不**为了好看而删掉那一半。
 */
export function ogMetaLine(route: TripRoute): string {
  const archetype = formatArchetype(route.archetype);
  const transit = formatTransit(route.transit_time_min, route.transit_distance_m);
  return `${archetype} · 站间交通 ${transit}`;
}

/**
 * 站点坐标 → 卡片示意图的归一化坐标（0–1）。
 *
 * 为什么是纯函数：示意图的画法要被单测钉住 —— 尤其单点行程（所有点重合，
 * 除数为 0）不能画出 `NaN`，nan 会让 satori 直接渲染失败。
 *
 * 纬度按 cos(lat) ≈ 0.92（广州）换算成与经度可比的单位：经纬度直接归一化
 * 会把南北向的行程纵向压扁。修正必须**分子分母同时做** —— 只除以修正后的
 * 跨度而分子不修正，坐标会逃出 [0,1]（这个 bug 被端点测试当场抓住）。
 *
 * `pad` 给轨迹留出边距，避免最左/最右的标记被裁掉一半。
 */
export function normalizeTrack(
  points: ReadonlyArray<{ latitude: number; longitude: number }>,
  pad = 0.18,
): Array<{ x: number; y: number }> {
  const usable = points.filter(
    (p) => Number.isFinite(p.latitude) && Number.isFinite(p.longitude),
  );
  if (usable.length === 0) return [];

  const lats = usable.map((p) => p.latitude);
  const lngs = usable.map((p) => p.longitude);
  const minLat = Math.min(...lats);
  const maxLat = Math.max(...lats);
  const minLng = Math.min(...lngs);
  const maxLng = Math.max(...lngs);
  const LAT_CORRECTION = 0.92;
  const latExtent = (maxLat - minLat) * LAT_CORRECTION;
  const lngExtent = maxLng - minLng;
  // 两个方向各占 span 的一格：宽的那个占满，窄的按真实比例留白。
  const span = Math.max(latExtent, lngExtent);

  if (span <= 0) {
    // 单点（或全部重合）：画在正中，不猜方向。
    return usable.map(() => ({ x: 0.5, y: 0.5 }));
  }
  return usable.map((p) => {
    const x = (p.longitude - minLng) / span;
    const y = 1 - ((p.latitude - minLat) * LAT_CORRECTION) / span;
    return {
      x: pad + (1 - 2 * pad) * x,
      y: pad + (1 - 2 * pad) * y,
    };
  });
}

/** JSON-LD 里每个站点的措辞：`N. 站名（到达–离开）`；时间缺失就只说站名。 */
function stopListItem(
  stop: TripRoute["stops"][number],
  index: number,
): string {
  const times =
    stop.arrive_time && stop.depart_time
      ? `（${stop.arrive_time}–${stop.depart_time}）`
      : "";
  return `${index + 1}. ${stop.name}${times}`;
}

/**
 * `schema.org/TouristTrip` JSON-LD（PRD AC-9.5）。
 *
 * 纪律与页面渲染一致：**没拿到的字段就不写**，绝不写空串 / 0 / 占位符 ——
 * 结构化数据里的空值会被消费方当成真数据。
 * 只列前 8 个站：结构化数据是给机器摘要用的，列表过长会被截断且无意义。
 */
export function tripJsonLd(trip: TripOut, pageUrl: string): Record<string, unknown> {
  const route = pickPrimaryRoute(trip);
  const stops = (route?.stops ?? []).slice(0, 8);

  const data: Record<string, unknown> = {
    "@context": "https://schema.org",
    "@type": "TouristTrip",
    name: ogTitle(trip),
    touristType: SITE_NAME,
  };
  if (trip.days > 0) data.numDays = trip.days;
  if (route !== null && route.place_count > 0) {
    data.itinerary = stops.map(stopListItem);
  }
  if (pageUrl !== "") data.url = pageUrl;
  return data;
}
