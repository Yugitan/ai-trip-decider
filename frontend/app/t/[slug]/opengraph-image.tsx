/**
 * 公开分享页的**动态 OG 图**（PRD AC-9.5 / §21：微信 / 微博 / Twitter 预览要有大图）。
 *
 * 用 Next 的文件约定：同段路由的 metadata 自动带上 `og:image` / `og:image:width` /
 * `og:image:alt`（及 twitter 对应项），不需要在页面里手写 `<meta>` ——
 * 手写必然和图片路由漂移。
 *
 * ★ 两条硬纪律 ★
 * 1. **读不到行程就说读不到**：404 / 网络故障画一张「链接不可用」的空卡，
 *    绝不画一份编造的「示例行程」—— OG 图被社交平台缓存得很久，
 *    一张假图会被转发到失效链接上，比没图更糟；
 * 2. **不把分数画上卡片**：`recommend_score` 解释不了（7 维权重 + 乘数），
 *    卡片只用代码算出来的站数 / 时长 / 预算（与对比表同一纪律）。
 *
 * 字体：不指定 `fonts` 时 satori 回退到内置字体，对 CJK 交由运行环境的
 * 系统字体解析（本机已实测能出正确的中文与站序示意图，见 RUNNING.md §8.10）。
 * 部署环境若缺 CJK 字体，中文会变豆腐块 —— 属部署配置问题，兜底措辞全是
 * 数字与 ASCII，仍可读。
 *
 * `force-dynamic`：与页面本身同一选择 —— 取消分享必须立刻失效，
 * 静态化的 OG 图会在取消后继续「活着」一段时间，那就等于骗人。
 */

import { ImageResponse } from "next/og";

import { getSharedTrip } from "@/lib/api";
import { OgCard, OgUnavailableCard } from "@/components/og-card";
import { OG_SIZE } from "@/lib/og";

export const alt = "分享的广州路线方案卡片";
export const size = OG_SIZE;
export const contentType = "image/png";
export const dynamic = "force-dynamic";

export default async function Image({
  params,
}: {
  params: Promise<{ slug: string }>;
}) {
  const { slug } = await params;

  let trip = null;
  const message = "这个分享链接已失效，或地址写错了。";
  try {
    trip = (await getSharedTrip(slug)).data;
  } catch {
    // 404（取消分享 / 链接写错）与网络故障共用一句话：
    // 对预览读者来说，两者都是「这份行程现在看不到」。
  }

  return new ImageResponse(
    trip === null ? <OgUnavailableCard message={message} /> : <OgCard trip={trip} />,
    { ...size },
  );
}
