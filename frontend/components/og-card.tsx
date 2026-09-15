/**
 * OG 卡片的**渲染层**：把纯数据画成 1200×630 的 JSX。
 *
 * 为什么单独一个文件：`opengraph-image.tsx` 跑在图片路由里，测试很难断言它；
 * 而这张卡片承载的恰恰是「不知道就说不知道」的契约 —— 措辞全部来自
 * `lib/og.ts`（被单测钉住），本文件只负责把字符串与坐标摆进布局。
 * jsdom 断言的是 DOM 结构与文本；像素级验证交给真实冒烟（无头 Chrome）。
 */

import type { TripOut, TripRoute } from "@/lib/api";
import {
  normalizeTrack,
  OG_SIZE,
  ogMetaLine,
  ogSubtitle,
  ogTitle,
  pickPrimaryRoute,
} from "@/lib/og";
import { SITE_NAME } from "@/lib/site";

/** 与 globals.css 的主题一致（satori 读不到 CSS 变量，取同值字面量）。 */
const COLORS = {
  ink: "#0b1220",
  inkSoft: "#47506b",
  inkFaint: "#8a93a8",
  sand: "#f7f4ef",
  shell: "#ffffff",
  teal: "#0f8a80",
  tealTint: "#e6f4f2",
  line: "#e6e2da",
} as const;

const RADIUS = 28;

function TrackDiagram({ route }: { route: TripRoute }) {
  const points = route.stops.map((s) => ({ latitude: s.latitude, longitude: s.longitude }));
  const track = normalizeTrack(points);
  if (track.length === 0) return null;

  const width = OG_SIZE.width - 128;
  const height = 260;

  return (
    <div style={{ display: "flex", position: "relative", width, height }}>
      <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} style={{ display: "flex" }}>
        {track.length > 1 ? (
          <path
            d={track.map((p, i) => `${i === 0 ? "M" : "L"} ${p.x * width} ${p.y * height}`).join(" ")}
            stroke={COLORS.ink}
            strokeWidth={3}
            strokeDasharray="7 8"
            fill="none"
            strokeLinecap="round"
          />
        ) : null}
        {track.map((p, i) => {
          const cx = p.x * width;
          const cy = p.y * height;
          const last = i === track.length - 1;
          const marker = last ? 15 : 12;
          return (
            <circle
              key={i}
              cx={cx}
              cy={cy}
              r={marker}
              fill={last ? COLORS.teal : COLORS.shell}
              stroke={COLORS.ink}
              strokeWidth={3}
            />
          );
        })}
      </svg>
      {/* 编号叠加在圆点上：SVG 的 <text> 在 satori 里不被支持
          （"<text> nodes are not currently supported"），HTML 文本没问题。 */}
      {track.map((p, i) => {
        const last = i === track.length - 1;
        const marker = last ? 15 : 12;
        const fontSize = last ? 18 : 14;
        return (
          <div
            key={`label-${i}`}
            style={{
              position: "absolute",
              left: p.x * width - marker,
              top: p.y * height - marker,
              width: marker * 2,
              height: marker * 2,
              fontSize,
              lineHeight: `${marker * 2}px`,
              textAlign: "center",
              color: last ? "#ffffff" : COLORS.ink,
              fontWeight: 700,
              // satori 规则：容器只有一个文本子节点也必须显式 display
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
            }}
          >
            {i + 1}
          </div>
        );
      })}
    </div>
  );
}

function BrandRow() {
  return (
    <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", width: "100%" }}>
      <div style={{ display: "flex", alignItems: "center", gap: 16 }}>
        <div
          style={{
            width: 44,
            height: 44,
            borderRadius: 14,
            background: COLORS.ink,
            color: COLORS.sand,
            fontSize: 24,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
          }}
        >
          路
        </div>
        <span style={{ fontSize: 30, color: COLORS.inkSoft }}>{SITE_NAME}</span>
      </div>
      <span style={{ fontSize: 24, color: COLORS.inkFaint }}>别人分享的行程</span>
    </div>
  );
}

export function OgCard({ trip }: { trip: TripOut }) {
  const route = pickPrimaryRoute(trip);
  return (
    <div
      style={{
        width: "100%",
        height: "100%",
        display: "flex",
        flexDirection: "column",
        justifyContent: "space-between",
        padding: 64,
        background: COLORS.sand,
        color: COLORS.ink,
        fontFamily: "system-ui",
      }}
    >
      <BrandRow />

      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        <div style={{ display: "flex", fontSize: 56, lineHeight: 1.15 }}>{ogTitle(trip)}</div>
        <div style={{ display: "flex", fontSize: 34, color: COLORS.inkSoft }}>{ogSubtitle(trip, route)}</div>
        {route !== null ? <TrackDiagram route={route} /> : null}
      </div>

      {route !== null ? (
        <div
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            width: "100%",
            background: COLORS.shell,
            borderRadius: RADIUS,
            border: `2px solid ${COLORS.line}`,
            padding: "28px 36px",
          }}
        >
          <div style={{ display: "flex", flexDirection: "column" }}>
            <span style={{ fontSize: 36, color: COLORS.ink }}>{route.name}</span>
            <span style={{ fontSize: 24, color: COLORS.inkSoft }}>{ogMetaLine(route)}</span>
          </div>
          <div
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              background: COLORS.tealTint,
              borderRadius: 999,
              padding: "10px 28px",
              fontSize: 30,
              color: COLORS.teal,
            }}
          >
            {route.label}
          </div>
        </div>
      ) : (
        <div
          style={{
            display: "flex",
            width: "100%",
            background: COLORS.shell,
            borderRadius: RADIUS,
            border: `2px solid ${COLORS.line}`,
            padding: "28px 36px",
            fontSize: 28,
            color: COLORS.inkSoft,
          }}
        >
          这份行程没有可展示的方案
        </div>
      )}
    </div>
  );
}

/** 失效链接的空卡：说真话的那一张（绝不画编造的行程）。 */
export function OgUnavailableCard({ message }: { message: string }) {
  return (
    <div
      style={{
        width: "100%",
        height: "100%",
        display: "flex",
        flexDirection: "column",
        justifyContent: "space-between",
        padding: 64,
        background: COLORS.sand,
        color: COLORS.ink,
        fontFamily: "system-ui",
      }}
    >
      <BrandRow />
      <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
        <div style={{ display: "flex", fontSize: 52 }}>这个分享链接不可用</div>
        <div style={{ display: "flex", fontSize: 30, color: COLORS.inkSoft }}>{message}</div>
      </div>
      <div
        style={{
          display: "flex",
          width: "100%",
          background: COLORS.shell,
          borderRadius: RADIUS,
          border: `2px solid ${COLORS.line}`,
          padding: "28px 36px",
          fontSize: 26,
          color: COLORS.inkFaint,
        }}
      >
        分享者取消分享后，链接会立刻失效 —— 图上也照实说。
      </div>
    </div>
  );
}
