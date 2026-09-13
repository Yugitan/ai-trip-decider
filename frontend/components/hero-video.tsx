"use client";

import { useEffect, useRef } from "react";

/**
 * 首页 Hero 的背景视频。
 *
 * 它不是 `<video loop>`：原生 loop 会在片尾硬切回开头，画面会"跳"一下。
 * 这里用 requestAnimationFrame 持续读 currentTime / duration，按时间算出透明度：
 *
 *   开始   0 → 1  （0.5s 淡入）
 *   结束   1 → 0  （0.5s 淡出）
 *   ended  opacity = 0 → 等 100ms → currentTime = 0 → play() → 再播一遍
 *
 * 目标是"几乎无感的循环"：接缝处是一次黑场，而不是一次跳帧。
 *
 * ★ 几个刻意的决定 ★
 * 1. **不写 `autoPlay`**：由本组件在确认用户没有要求减少动效之后再开始播放。
 *    浏览器在 DOM 里插上 `<video autoplay>` 时会立刻播放，等到 effect 里再拦就晚了 ——
 *    `prefers-reduced-motion: reduce` 的用户会先看到画面动起来。
 * 2. **只在真正拿得到数据时才 play()**（`loadedmetadata` / `readyState >= 1`）。
 *    jsdom 里 `play()` / `pause()` 都是 not-implemented 会往 stderr 打警告，
 *    而二者都只在真实媒体事件到达后才会被调用 —— 测试日志保持干净。
 * 3. **卸载时不调用 `pause()`**：React 卸载会把 `<video>` 节点整个移出文档，
 *    浏览器会随之停止播放并释放资源；显式 pause() 反而会触发上面那条 jsdom 警告。
 * 4. 只动 `opacity`（合成层），不动 width/height —— 避免逐帧重排。
 *
 * 页面切换（切到后台标签页）：浏览器会节流 rAF，但透明度是**按绝对时间算出来的**，
 * 所以切回来时画面不会"停在半透明"或突然跳变；切后台时若正好片尾，
 * 重播的 setTimeout 会被浏览器拖到 ≥1s，那一秒只是黑场比预期长一点，不会卡死。
 */

/** 内置默认背景视频。可用 `NEXT_PUBLIC_HERO_VIDEO_URL` 指向自己的资源。 */
export const DEFAULT_HERO_VIDEO_SRC =
  "https://d8j0ntlcm91z4.cloudfront.net/user_38xzZboKViGWJOttwIXH07lWA1P/hf_20260328_083109_283f3553-e28f-428b-a723-d639c617eb2b.mp4";

/**
 * 环境变量里配了非空值就用它，否则回落到内置资源。
 * 空串要当成「没配」：`.env` 里 `NEXT_PUBLIC_HERO_VIDEO_URL=` 这种写法很常见，
 * 若直接 `??` 会得到 `src=""` —— 一个不留神就变成不发任何请求的空视频。
 */
function resolveVideoSrc(raw: string | undefined): string {
  const trimmed = raw?.trim();
  return trimmed !== undefined && trimmed.length > 0 ? trimmed : DEFAULT_HERO_VIDEO_SRC;
}

export const HERO_VIDEO_SRC = resolveVideoSrc(
  process.env.NEXT_PUBLIC_HERO_VIDEO_URL,
);

/** 片头淡入 / 片尾淡出的时长（秒）。 */
export const HERO_VIDEO_FADE_S = 0.5;

/** 循环之间留出的黑场（毫秒）。 */
export const HERO_VIDEO_RESTART_DELAY_MS = 100;

/**
 * 某一时刻的氛围透明度（纯函数，便于单测）。
 *
 * 取「淡入进度」与「淡出进度」的较小值，于是两端各自线性过渡、
 * 中间稳定在 1；时长还不可知（NaN / 0）时返回 0 —— 宁可先不显示，
 * 也不要在拿到元数据前闪一下整帧画面。
 */
export function heroVeilOpacity(currentTime: number, duration: number): number {
  if (!Number.isFinite(duration) || duration <= 0) return 0;
  if (!Number.isFinite(currentTime)) return 0;
  const fadeIn = currentTime / HERO_VIDEO_FADE_S;
  const fadeOut = (duration - currentTime) / HERO_VIDEO_FADE_S;
  return Math.max(0, Math.min(1, fadeIn, fadeOut));
}

function prefersReducedMotion(): boolean {
  return (
    typeof window.matchMedia === "function" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches
  );
}

export function HeroVideo({
  src = HERO_VIDEO_SRC,
  className = "",
}: {
  src?: string;
  className?: string;
}) {
  const videoRef = useRef<HTMLVideoElement>(null);

  useEffect(() => {
    const video = videoRef.current;
    if (!video) return;
    // 减少动效：不开始播放，也不启动 RAF —— 背景保持静态渐变纱幕。
    if (prefersReducedMotion()) return;

    let frameId = 0;
    // 用 `window.setTimeout`（浏览器返回 number）而不是全局 `setTimeout`：
    // 后者的返回类型在 Node 类型定义下是 Timeout 对象，与 clearTimeout 的类型对不上。
    let restartId: number | null = null;
    let disposed = false;

    /** 逐帧把 opacity 收敛到 heroVeilOpacity 算出的值。 */
    const paint = () => {
      frameId = 0;
      if (disposed) return;
      video.style.opacity = String(heroVeilOpacity(video.currentTime, video.duration));
      if (!video.ended) frameId = window.requestAnimationFrame(paint);
    };

    const startFrames = () => {
      if (disposed || frameId !== 0) return;
      frameId = window.requestAnimationFrame(paint);
    };

    const stopFrames = () => {
      if (frameId === 0) return;
      window.cancelAnimationFrame(frameId);
      frameId = 0;
    };

    const playOnce = () => {
      if (disposed || !video.paused) return;
      // 同步 throw（策略拒绝/元素损坏）与异步 reject 都要兜住：
      // 背景视频失败是纯装饰性降级，不该影响页面。
      try {
        const attempt = video.play();
        if (attempt !== undefined && typeof attempt.catch === "function") {
          attempt.catch(() => {
            video.style.opacity = "0";
          });
        }
      } catch {
        video.style.opacity = "0";
      }
    };

    const handleReady = () => {
      playOnce();
      startFrames();
    };

    const handleEnded = () => {
      if (disposed) return;
      stopFrames();
      video.style.opacity = "0";
      // 先黑场 100ms 再回到起点重播，接缝才不会被看见
      restartId = window.setTimeout(() => {
        restartId = null;
        if (disposed) return;
        video.currentTime = 0;
        playOnce();
      }, HERO_VIDEO_RESTART_DELAY_MS);
    };

    const handlePlaying = () => startFrames();
    const handlePause = () => stopFrames();

    video.addEventListener("loadedmetadata", handleReady);
    video.addEventListener("loadeddata", startFrames);
    video.addEventListener("playing", handlePlaying);
    video.addEventListener("pause", handlePause);
    video.addEventListener("ended", handleEnded);

    // 命中 bfcache / 同页重挂载时元数据可能已经到了，不必再等事件。
    if (video.readyState >= 1) handleReady();

    return () => {
      disposed = true;
      stopFrames();
      if (restartId !== null) window.clearTimeout(restartId);
      video.removeEventListener("loadedmetadata", handleReady);
      video.removeEventListener("loadeddata", startFrames);
      video.removeEventListener("playing", handlePlaying);
      video.removeEventListener("pause", handlePause);
      video.removeEventListener("ended", handleEnded);
    };
  }, [src]);

  return (
    <video
      ref={videoRef}
      src={src}
      className={className}
      // 必须 muted 才可能自动播放；没有音轨，也不允许用户打开声音
      muted
      playsInline
      preload="metadata"
      aria-hidden="true"
      tabIndex={-1}
      style={{ opacity: 0 }}
    />
  );
}
