import type { Metadata, Viewport } from "next";

import { siteUrl } from "@/lib/site";

import "./globals.css";

export const metadata: Metadata = {
  /**
   * metadataBase：把相对的 og:image / og:url 解析成绝对地址。
   * 不设的话 Next 会按部署平台猜（Vercel 上猜对，自托管猜错），
   * 且开发期会打一条 "metadataBase not set" 的警告。
   * 值只有一个出处：`lib/site.ts` 的 `SITE_URL`。
   */
  metadataBase: new URL(siteUrl()),
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
