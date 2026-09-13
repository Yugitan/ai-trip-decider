import type { Metadata } from "next";
import Link from "next/link";

import { DevSettings } from "@/components/dev-settings";
import { SiteFooter } from "@/components/site-footer";
import { SiteHeader } from "@/components/site-header";

/**
 * 开发设置页（`/dev`）—— **不是用户功能**。
 *
 * 为什么单独成页而不是塞进首页：这个面板能改 `.env` 与 `config/*.yaml`，
 * 放哪里都会被误当成产品功能。「一个只在开发时存在的页面 + 头部一个只在开发时出现的入口」
 * 是最省事的诚实做法：生产构建里入口根本不渲染，就算有人猜到地址，
 * 后端 `/api/v1/dev/*` 也不会注册（404）。
 */

export const metadata: Metadata = {
  title: "开发设置",
  description: "开发期配置面板：LLM / 搜索 / 地图 / 成本阈值与 config/*.yaml。",
  // 别让搜索引擎收录一个开发工具页
  robots: { index: false, follow: false },
};

export default function DevSettingsPage() {
  return (
    <div className="min-h-dvh bg-sand">
      <SiteHeader current="dev" />

      <main className="mx-auto w-full max-w-5xl px-5 pt-10 pb-24 sm:px-8">
        <Link
          href="/"
          className="inline-flex min-h-11 items-center text-sm text-teal-dark transition-colors duration-300 hover:text-ink"
        >
          ← 回到规划
        </Link>

        <div className="mt-4">
          <DevSettings />
        </div>
      </main>

      <SiteFooter />
    </div>
  );
}
