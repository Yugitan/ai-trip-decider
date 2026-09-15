"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { SiteFooter } from "@/components/site-footer";
import { SiteHeader } from "@/components/site-header";
import {
  forgetRecentTrips,
  formatSavedAt,
  readRecentTrips,
  type RecentTrip,
} from "@/lib/recent-trips";

/**
 * `/me` —— 我的行程（PRD §7.1 路由表：CSR + LocalStorage）。
 *
 * ★ 这一页为什么必须存在 ★
 * 在此之前，行程只活在两处：首页表单下方的内嵌结果（刷新就没了），
 * 以及"你自己复制下来"的 `/trip/{id}` 地址。用户建过哪几份行程，产品自己是不记得的 ——
 * 于是"我昨天规划的那份呢"只能靠浏览器历史去翻。这一页把这件事变成一个地址。
 *
 * ★ 三条刻意的选择 ★
 * 1. **客户端渲染**：记录只在浏览器里（localStorage），服务端渲染读不到它，
 *    硬要在服务端读就得把用户数据送到服务器 —— 那正好和"只存在本地"这件事相反；
 * 2. **首帧是"正在读取"，不是"还没有行程"**：`useState<RecentTrip[] | null>(null)`，
 *    读完之后才判断空状态。初始化成空数组会让首帧先说一句"你还没有行程"
 *    （内容跳动之外，那还是一句**假话**）；
 * 3. **不假装记录 = 行程**：本地只存 id 与标题，行程本身仍在后端、按游客会话授权。
 *    会话 cookie 没了（换浏览器、清站点数据）这些链接就会打不开 ——
 *    与其让用户自己撞 403，不如在这里先说明白。
 *
 * 记录的**写入**在 `trip-workspace.tsx`（取回一份行程成功时），
 * 因为"这一版是什么"只由接口返回的那份数据决定。
 */

const SHELL = "mx-auto w-full max-w-3xl px-5 sm:px-8";

export default function MyTripsPage() {
  /** `null` = 还没读到（首帧）。见文件头第 2 条。 */
  const [trips, setTrips] = useState<RecentTrip[] | null>(null);

  useEffect(() => {
    setTrips(readRecentTrips());
  }, []);

  function handleClear() {
    forgetRecentTrips();
    setTrips([]);
  }

  return (
    <>
      <SiteHeader current="me" />
      <main className={`${SHELL} pt-12 pb-20`}>
        <p className="text-xs tracking-[0.14em] text-ink-soft uppercase">只在这台浏览器里</p>
        <h1 className="mt-4 text-3xl leading-[1.1] tracking-[-0.01em] text-ink sm:text-4xl">
          我的行程
        </h1>
        <p className="mt-4 max-w-2xl text-sm leading-relaxed text-ink-soft">
          这里列出你在这台浏览器上建过或打开过的行程。记录存在浏览器本地，
          <span className="font-medium text-ink">不经过服务器</span>
          —— 清掉浏览器数据就会一起消失。
        </p>
        <p className="mt-2 max-w-2xl text-xs leading-relaxed text-ink-faint">
          记录里只有行程的地址与标题，行程内容仍在后端、按游客会话隔离。
          换了浏览器或清过站点数据之后，这些链接可能打不开 —— 那时后端会直接告诉你。
        </p>

        <div className="mt-8">
          {trips === null ? (
            <p
              data-testid="recent-trips-loading"
              className="rounded-[10px] border border-line bg-shell/60 px-4 py-3 text-xs leading-relaxed text-ink-soft"
            >
              正在读取本地记录…
            </p>
          ) : trips.length === 0 ? (
            <div
              data-testid="recent-trips-empty"
              className="rounded-card border border-line bg-shell p-6"
            >
              <h2 className="text-lg text-ink">这里还是空的</h2>
              <p className="mt-2 text-sm leading-relaxed text-ink-soft">
                还没有规划过任何行程 —— 提交一次需求（表单有默认值，不填也能提交），
                拿到 A/B/C 三套取舍不同的广州路线之后，它就会出现在这里。
              </p>
              <Link
                href="/#planner"
                className="mt-4 inline-flex min-h-11 items-center justify-center rounded-full bg-ink px-6 text-sm text-sand transition-transform duration-300 hover:scale-[1.03] motion-reduce:hover:scale-100"
              >
                去规划一次
              </Link>
            </div>
          ) : (
            <>
              <ol data-testid="recent-trips-list" className="space-y-2">
                {trips.map((trip) => (
                  <li
                    key={trip.id}
                    data-testid={`recent-trip-${trip.id}`}
                    className="rounded-[10px] border border-line bg-shell p-3.5"
                  >
                    <Link
                      href={`/trip/${encodeURIComponent(trip.id)}`}
                      className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1 text-ink"
                    >
                      <span className="text-sm font-medium">{trip.title}</span>
                      <span className="tnum text-xs text-ink-faint">
                        {formatSavedAt(trip.savedAt) || "时间未知"}
                      </span>
                    </Link>
                  </li>
                ))}
              </ol>
              <div className="mt-4 flex flex-wrap items-center gap-3">
                <button
                  type="button"
                  onClick={handleClear}
                  data-testid="recent-trips-clear"
                  className="inline-flex min-h-11 items-center justify-center rounded-full border border-line bg-shell px-5 text-sm text-ink-soft transition-colors duration-300 hover:border-coral/40 hover:text-coral"
                >
                  清空这些记录
                </button>
                <p className="text-xs leading-relaxed text-ink-faint">
                  只删本地这份清单，行程本身（以及已经生成的公开链接）不受影响。
                </p>
              </div>
            </>
          )}
        </div>

        <p className="mt-8 text-xs leading-relaxed text-ink-faint">
          想给别人看某一版？打开它，用页面里的「分享这套路线」生成公开链接 ——
          拿到链接的人不需要登录。
        </p>
      </main>
      <SiteFooter />
    </>
  );
}
