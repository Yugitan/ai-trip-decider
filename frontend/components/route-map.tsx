"use client";

import { useEffect, useRef, useState, type ReactNode } from "react";

import { isPlottable, wgs84ToGcj02, type MapPoint } from "@/lib/amap-coords";
import {
  amapJsKey,
  loadAmap,
  type AmapMap,
  type AmapOverlay,
} from "@/lib/amap";

/**
 * 一张小地图：把某套方案的站点按顺序标出来。
 *
 * ★ 三条渲染纪律（与后端 `TripStop` 的字段边界一一对应）★
 * 1. **坐标要纠偏**：库里的坐标是 WGS-84（来自 OSM），高德是 GCJ-02 ——
 *    不换就是每个点偏 100–700 米（详见 `lib/amap-coords.ts`）；
 * 2. **连线只表示顺序**：我们手上只有站点坐标，**没有路段几何**，
 *    所以那条虚线只说明「谁在谁前面」，图注里必须写清楚它不是实际路线 ——
 *    画一条像马路一样的线却不说明，就是让用户以为我们有路线数据；
 * 3. **画不出来就说明白**：缺 Key / 脚本加载失败 / 坐标不全会各给一句真话，
 *    而不是留一个灰盒子，或者只画一半的点位。
 *
 * ★ 每套方案各一张（最多 3 张）★
 * 高德 2.0 每个地图实例占一个 WebGL 上下文，3 张在桌面与移动端都还安全。
 * 方案数再多、或页面里出现第二处地图时，就该改成「共享一张大地图 + 切换方案」，
 * 那时这个组件要往上提一层 —— 现在不做，是因为 3 张不值得那个复杂度。
 *
 * 站点列表（同一张卡片里的 `<ol>`）才是可读的完整信息，地图是它的补充：
 * 所以容器对读屏软件隐藏，图注负责把地图想说的话说成文字。
 */
export type RouteMapState =
  | { kind: "loading" }
  | { kind: "ready" }
  | { kind: "unconfigured" }
  | { kind: "failed"; reason: string }
  | { kind: "empty" };

export function RouteMap({
  points,
  label,
}: {
  points: MapPoint[];
  label: string;
}) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [state, setState] = useState<RouteMapState>({ kind: "loading" });

  // 依赖用「点的指纹」而不是数组本身：每次渲染 `points` 都是新数组，
  // 直接进依赖数组会让 effect 每帧重建地图（并反复销毁上一个 WebGL 上下文）。
  const signature = points
    .map((point) => `${point.seq}:${point.latitude}:${point.longitude}`)
    .join("|");
  // 地图 effect 要读最新的点，但不该因为「数组换了身份」而重跑 —— 走 ref。
  // 在 effect 里同步（而不是渲染期间赋值）：渲染期间写 ref 在并发渲染下不安全。
  const pointsRef = useRef(points);
  useEffect(() => {
    pointsRef.current = points;
  }, [points]);

  useEffect(() => {
    const plottable = pointsRef.current.filter((point) =>
      isPlottable(point.latitude, point.longitude),
    );
    if (plottable.length === 0) {
      setState({ kind: "empty" });
      return;
    }
    if (amapJsKey() === null) {
      setState({ kind: "unconfigured" });
      return;
    }

    let cancelled = false;
    let map: AmapMap | null = null;
    setState({ kind: "loading" });

    void (async () => {
      const result = await loadAmap();
      if (cancelled) return;
      if (!result.ok) {
        setState({ kind: "failed", reason: result.reason });
        return;
      }
      const container = containerRef.current;
      if (container === null) return;

      const amap = result.amap;
      try {
        map = new amap.Map(container, { zoom: 12, viewMode: "2D" });

        const overlays: AmapOverlay[] = [];
        const path = plottable.map((point) => {
          // ★ 唯一一处坐标变换：存储与计算全在 WGS-84，只在这里换成 GCJ-02 ★
          const converted = wgs84ToGcj02(point.latitude, point.longitude);
          return new amap.LngLat(converted.longitude, converted.latitude);
        });

        plottable.forEach((point, index) => {
          overlays.push(
            new amap.Marker({
              position: path[index],
              title: point.name,
              label: { content: String(point.seq), direction: "top" },
            }),
          );
        });

        if (path.length > 1) {
          overlays.push(
            new amap.Polyline({
              path,
              strokeColor: "#0f766e",
              strokeWeight: 3,
              strokeOpacity: 0.85,
              strokeStyle: "dashed",
            }),
          );
        }

        map.add(overlays);
        map.setFitView(overlays, true);
        if (!cancelled) setState({ kind: "ready" });
      } catch (cause: unknown) {
        // 容器没有尺寸、WebGL 不可用等情况下高德会直接抛：转成一句人话。
        if (!cancelled) {
          setState({
            kind: "failed",
            reason: `地图初始化失败（${
              cause instanceof Error ? cause.message : "未知原因"
            }）`,
          });
        }
      }
    })();

    return () => {
      cancelled = true;
      map?.destroy();
    };
  }, [signature]);

  const drawing = state.kind === "loading" || state.kind === "ready";

  return (
    <figure className="mt-3">
      {state.kind === "unconfigured" ? (
        <MapNote>
          未配置地图 Key（frontend/.env.local 的 NEXT_PUBLIC_AMAP_JS_KEY）——
          上面的站点列表就是全部信息，位置示意要配置后才会显示。
        </MapNote>
      ) : null}

      {state.kind === "empty" ? (
        <MapNote>
          这套方案里有站点缺少可用坐标，画不出地图。站点本身照常显示在上面，
          界面不会替它们编一个位置。
        </MapNote>
      ) : null}

      {state.kind === "failed" ? (
        <MapNote>{state.reason}。站点列表不受影响。</MapNote>
      ) : null}

      {state.kind === "loading" ? <MapNote>地图加载中…</MapNote> : null}

      {drawing ? (
        <div
          ref={containerRef}
          data-testid={`route-map-${label}`}
          className="h-[220px] w-full overflow-hidden rounded-[10px] border border-line bg-sand-wash sm:h-[260px]"
          // 同一份信息在站点列表里已有文字版本；这里对读屏软件隐藏，
          // 说明职责交给下面的图注。
          aria-hidden="true"
        />
      ) : null}

      {state.kind === "ready" ? (
        <figcaption className="mt-1.5 text-[11px] leading-relaxed text-ink-faint">
          {`站点位置示意图（方案 ${label}） · 虚线只表示站点的先后顺序，不是实际行车路线；显示坐标已按高德坐标系纠偏。`}
        </figcaption>
      ) : null}
    </figure>
  );
}

/** 地图画不出来时统一用它 —— 样式一致，且每句都补上「站点列表不受影响」。 */
function MapNote({ children }: { children: ReactNode }) {
  return (
    <p className="rounded-[10px] border border-line bg-sand-wash px-3 py-2 text-[11px] leading-relaxed text-ink-soft">
      {children}
    </p>
  );
}
