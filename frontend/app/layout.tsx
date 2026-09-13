import type { Metadata, Viewport } from "next";

import "./globals.css";

export const metadata: Metadata = {
  title: {
    default: "TripDecider · 你负责决定怎么玩，路线交给我",
    template: "%s — TripDecider",
  },
  description:
    "AI 旅行路线决策器：告诉我目的地、时间、人数、偏好、预算和节奏，我给你 2–3 套经过可行性校验、可比价、能继续改的路线方案 —— 而不是一篇攻略。",
  keywords: ["广州旅游", "广州一日游", "路线规划", "行程规划", "CityWalk", "广州美食"],
  applicationName: "TripDecider",
  openGraph: {
    title: "TripDecider · 你负责决定怎么玩，路线交给我",
    description: "2–3 套经过校验的广州路线方案，可比价、可继续改。",
    type: "website",
    locale: "zh_CN",
  },
  robots: { index: true, follow: true },
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  // 不锁定缩放：无障碍要求允许用户放大
  maximumScale: 5,
  themeColor: "#f7f4ef",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="zh-CN">
      <body className="min-h-dvh antialiased">{children}</body>
    </html>
  );
}
