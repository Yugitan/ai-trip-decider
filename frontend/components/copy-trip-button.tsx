"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

import { copyPublicTrip } from "@/lib/api";
import { describeFailure, FailureBlock, type FailureInfo } from "@/components/trip-result";
import { Button } from "@/components/ui/button";

/**
 * 分享页的「复制这套路线」（PRD AC-9.4）。
 *
 * ★ 它补上的是分享机制里最容易漏掉的一半 ★
 * 分享页一直是**只读**的：看完就走，或者回首页重新填一遍需求。后端的
 * `POST /public/trips/{slug}/copy` 早就可用（生成一份属于当前访客的副本，原行程不受影响），
 * 但前端没有落脚点 —— 一个没有入口的接口等于没有。
 *
 * 三条要说清楚的事（都直接写在界面上）：
 * 1. **复制，不是接管**：改副本不会影响分享者的那一份；
 * 2. **副本属于这个浏览器**：行程按游客会话隔离，不需要登录，但换一个浏览器就看不到了；
 * 3. 复制成功后**直接去 `/trip/{id}`** —— 副本只有在有地址、能改的地方才算"拿到手"。
 */

export function CopyTripButton({ slug }: { slug: string }) {
  const router = useRouter();
  const [copying, setCopying] = useState(false);
  const [copied, setCopied] = useState(false);
  const [failure, setFailure] = useState<FailureInfo | null>(null);

  async function handleCopy() {
    if (copying) return;
    setCopying(true);
    setFailure(null);

    try {
      const result = await copyPublicTrip(slug);
      setCopied(true);
      // 副本有它自己的 trip_id，`/trip/{id}` 才是能继续改的地方（那一页也自带地址）。
      router.push(`/trip/${result.data.trip_id}`);
    } catch (cause: unknown) {
      // 失败的常见原因是分享被取消了（后端直接 404）—— 如实转述后端的话，
      // 不写"网络错误"这类会把人引向错误方向的说法。
      setFailure(describeFailure(cause, "没能复制这套路线"));
    } finally {
      setCopying(false);
    }
  }

  return (
    <div
      data-testid="copy-trip"
      className="rounded-card border border-line bg-shell/60 p-6"
    >
      <h2 className="text-lg text-ink">想按这条线走？复制成你自己的一份</h2>
      <p className="mt-2 text-sm leading-relaxed text-ink-soft">
        复制出来的是一份
        <span className="font-medium text-ink">属于你这个浏览器</span>
        的副本（不需要登录）：可以改路线、撤销、再分享。改动只作用在副本上，
        分享者的那一份不会变。
      </p>

      <div className="mt-4 flex flex-wrap items-center gap-3">
        <Button loading={copying} onClick={() => void handleCopy()}>
          {copying ? "正在复制…" : copied ? "已复制" : "复制这套路线"}
        </Button>
        <p className="text-xs leading-relaxed text-ink-faint">
          复制后会打开你自己的那一页（地址形如 <code>/trip/…</code>）。
        </p>
      </div>

      <div aria-live="polite">
        {failure === null ? null : (
          <div
            data-testid="copy-error"
            className="mt-3 rounded-[10px] border border-coral/40 bg-coral-tint/70 px-3 py-2.5"
          >
            <FailureBlock failure={failure} />
          </div>
        )}
      </div>
    </div>
  );
}
