import type { Metadata } from "next";
import Link from "next/link";

import { ApiError, getSharedTrip, type TripOut } from "@/lib/api";
import { tripJsonLd } from "@/lib/og";
import { siteUrl } from "@/lib/site";
import { CopyTripButton } from "@/components/copy-trip-button";
import { SiteFooter } from "@/components/site-footer";
import { SiteHeader } from "@/components/site-header";
import { TripResultView } from "@/components/trip-result";

/**
 * 公开分享页 `GET /api/v1/public/trips/{slug}`（PRD FR-09 / AC-9.3）。
 *
 * ★ 为什么这一页必须存在 ★
 * 后端的 `POST /trips/{id}/share` 返回的是 `{frontend_url}/t/{slug}`。
 * 在它存在之前，那个链接是一个**死链** —— 界面上"生成分享链接"能成功，
 * 但点开是 404。分享功能的价值全在"别人能打开"，所以这一页不是锦上添花。
 *
 * 四个刻意的选择：
 * 1. **服务端渲染**：拿的是 `getSharedTrip`（无鉴权、只返回已公开的行程），
 *    所以任何浏览器打开都能看到内容，不依赖 cookie —— 这正是它与 `/trips/{id}` 的区别。
 * 2. **`force-dynamic`**：取消分享必须**立刻**失效（AC-9.7）。
 *    一旦静态化/缓存，取消之后链接还会继续可用一段时间 —— 那就等于骗人。
 * 3. **只读**：没有修改/撤销/分享按钮 —— 但提供「复制这套路线」（`CopyTripButton`，AC-9.4）：
 *    它把这份行程复制成**访客自己**的一份可编辑副本，然后带去 `/trip/{id}`。
 *    复制出来的东西归访客，这一页上的原行程仍然动不了。
 * 4. **社交预览**（AC-9.5）：`og:image` 来自同路由的 `opengraph-image.tsx`
 *    （文件约定自动接线，手写必然漂移）；结构化数据用 `schema.org/TouristTrip`
 *    JSON-LD，由 `tripJsonLd()` 生成 —— 没拿到的字段就不写，绝不占位。
 */

export const dynamic = "force-dynamic";

interface PageProps {
  params: Promise<{ slug: string }>;
}

const SHELL = "mx-auto w-full max-w-4xl px-5 sm:px-8";

/**
 * 标题里带上行程名，分享出去才像样。
 * 代价是多一次请求：`request()` 用 `cache: "no-store"`，Next 不会替我们合并这两次
 * （`opengraph-image.tsx` 里还有第三次）。换来的是预览标题与卡片都用真数据。
 */
export async function generateMetadata({ params }: PageProps): Promise<Metadata> {
  const { slug } = await params;
  try {
    const { data } = await getSharedTrip(slug);
    const title = data.title ? `${data.title} · 分享的路线` : "分享的路线";
    const description = `${data.days} 天 · ${data.route_count} 套方案（广州）`;
    return {
      title,
      description,
      // og:image 不在这里声明：同路由的 opengraph-image.tsx（文件约定）
      // 会自动补全 og:image / og:image:width / og:image:alt 及 twitter 对应项 ——
      // 手写一个 images 数组反而会**顶掉**文件约定的自动填充（实测过：
      // images 里写 url:"" 之后 og:image 从 head 里消失，冒烟抓到过这个回归）。
      openGraph: { title, description, type: "article" },
      twitter: { title, description },
    };
  } catch {
    return { title: "这个分享链接不可用", robots: { index: false, follow: false } };
  }
}

export default async function SharedTripPage({ params }: PageProps) {
  const { slug } = await params;

  let trip: TripOut | null = null;
  let failure: { title: string; message: string; hint: string | null } | null = null;

  try {
    trip = (await getSharedTrip(slug)).data;
  } catch (error: unknown) {
    // 404 / SHARE_NOT_FOUND 是最常见的情况（取消分享、链接写错、被清理过）。
    // 刻意**不**把它渲染成"出错了"：对读者来说这是"这个链接没了"，不是系统故障。
    if (error instanceof ApiError) {
      failure = {
        title: "这个分享链接打不开了",
        message: error.message,
        hint: error.hint === "" ? null : error.hint,
      };
    } else {
      failure = {
        title: "没能读到这份分享的行程",
        message: "暂时连不上规划服务。",
        hint: "稍后刷新重试；如果一直打不开，可能是链接已经失效。",
      };
    }
  }

  const pageUrl = `${siteUrl()}/t/${encodeURIComponent(slug)}`;

  return (
    <>
      <SiteHeader />
      <main className={`${SHELL} pt-12 pb-20`}>
        <p className="text-xs tracking-[0.14em] text-ink-soft uppercase">别人分享的行程</p>
        <h1 className="mt-4 text-3xl leading-[1.1] tracking-[-0.01em] text-ink sm:text-4xl">
          这份路线是别人规划好的
        </h1>
        <p className="mt-4 max-w-2xl text-sm leading-relaxed text-ink-soft">
          下面是原样的行程快照：站点顺序、停留时间、站间交通与预算，以及它带着的不确定性。
          分享者取消分享后，这个链接会立刻失效。
        </p>

        {/* 结构化数据（AC-9.5）：给社交平台与搜索引擎的机器摘要。
            读不到行程时**不输出** —— 空的 JSON-LD 比没有更糟。 */}
        {trip === null ? null : (
          <script
            type="application/ld+json"
            // 数据由 tripJsonLd 从接口字段构造，name/url 均为文本或我们自己拼的 URL。
            dangerouslySetInnerHTML={{ __html: JSON.stringify(tripJsonLd(trip, pageUrl)) }}
          />
        )}

        <div className="mt-8">
          {trip === null ? (
            <div
              data-testid="share-unavailable"
              className="rounded-card border border-coral/25 bg-coral-tint/50 p-6"
            >
              <h2 className="text-lg text-ink">{failure?.title ?? "这个分享链接不可用"}</h2>
              <p className="mt-2 text-sm text-ink-soft">{failure?.message}</p>
              {failure?.hint ? (
                <p className="mt-1 text-xs text-ink-faint">{failure.hint}</p>
              ) : null}
              <p className="mt-4 text-xs text-ink-faint">
                这一页不显示任何占位行程 —— 读不到就说读不到。
              </p>
            </div>
          ) : (
            <TripResultView trip={trip} />
          )}
        </div>

        {/* 复制只在读到行程时才有意义：读不到就没有可复制的东西，也不该给一个只会失败的按钮。 */}
        {trip === null ? null : <div className="mt-10"><CopyTripButton slug={slug} /></div>}

        <div className="mt-6 rounded-card border border-line bg-shell/60 p-6">
          <h2 className="text-lg text-ink">也想让别人给你排一条？</h2>
          <p className="mt-2 text-sm leading-relaxed text-ink-soft">
            填一份需求（有默认值，不填也能提交），拿到 A/B/C 三套取舍不同的广州路线。
          </p>
          <Link
            href="/#planner"
            className="mt-4 inline-flex min-h-11 items-center justify-center rounded-full bg-ink px-6 text-sm text-sand transition-transform duration-300 hover:scale-[1.03] motion-reduce:hover:scale-100"
          >
            我也要规划一次
          </Link>
        </div>
      </main>
      <SiteFooter />
    </>
  );
}
