/**
 * `HeroVideo` 的测试。
 *
 * 为什么值得测一个"背景视频"：
 * 1. 它自己实现了一套循环（不用原生 `loop`）—— 淡入淡出、片尾黑场、重播，
 *    全都是容易写错又不容易被肉眼发现（只是"接缝跳一下"）的逻辑。
 * 2. 它必须**尊重 `prefers-reduced-motion`**，而且必须在开始播放**之前**就判断好：
 *    如果先播放再拦住，用户已经看到画面动了。
 * 3. jsdom 里 `HTMLMediaElement.play()/pause()` 是 not-implemented 会往 stderr 打警告，
 *    所以组件刻意只在真实媒体事件（`loadedmetadata` 等）到达时才调用它们 ——
 *    下面的用例会把这个契约钉住：**JS 里不许主动 play/pause**。
 */

import { act, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  HERO_VIDEO_FADE_S,
  HERO_VIDEO_RESTART_DELAY_MS,
  HERO_VIDEO_SRC,
  HeroVideo,
  heroVeilOpacity,
} from "@/components/hero-video";

// ── 纯度可测的那部分：透明度曲线 ─────────────────────────────────────────────

describe("heroVeilOpacity", () => {
  it("时长还不可知时不显示任何画面（宁可先黑着，也不要闪一帧）", () => {
    expect(heroVeilOpacity(0, Number.NaN)).toBe(0);
    expect(heroVeilOpacity(3, 0)).toBe(0);
    expect(heroVeilOpacity(Number.NaN, 10)).toBe(0);
  });

  it(`片头 ${HERO_VIDEO_FADE_S}s 线性淡入、片尾 ${HERO_VIDEO_FADE_S}s 线性淡出`, () => {
    expect(heroVeilOpacity(0, 10)).toBe(0);
    expect(heroVeilOpacity(HERO_VIDEO_FADE_S / 2, 10)).toBe(0.5);
    expect(heroVeilOpacity(HERO_VIDEO_FADE_S, 10)).toBe(1);
    expect(heroVeilOpacity(5, 10)).toBe(1);
    expect(heroVeilOpacity(10 - HERO_VIDEO_FADE_S / 2, 10)).toBe(0.5);
    expect(heroVeilOpacity(10, 10)).toBe(0);
  });

  it("短于淡入淡出时长的视频也不会算出 >1 或 <0 的值", () => {
    expect(heroVeilOpacity(0.2, 0.4)).toBeLessThanOrEqual(1);
    expect(heroVeilOpacity(0.2, 0.4)).toBeGreaterThanOrEqual(0);
  });
});

describe("背景视频的资源配置", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.resetModules();
    vi.unstubAllGlobals();
  });

  it("默认指向内置资源（没有任何环境变量时也能跑）", async () => {
    vi.stubEnv("NEXT_PUBLIC_HERO_VIDEO_URL", "");
    vi.resetModules();
    const mod = await import("@/components/hero-video");

    expect(mod.HERO_VIDEO_SRC).toBe(mod.DEFAULT_HERO_VIDEO_SRC);
  });

  it("★ 空串 / 纯空白按「没配置」处理，而不是产出一个 src=\"\" 的空视频", async () => {
    vi.stubEnv("NEXT_PUBLIC_HERO_VIDEO_URL", "   ");
    vi.resetModules();
    const mod = await import("@/components/hero-video");

    expect(mod.HERO_VIDEO_SRC).toBe(mod.DEFAULT_HERO_VIDEO_SRC);
  });

  it("配了地址就用配的（顺手去掉首尾空白）", async () => {
    vi.stubEnv("NEXT_PUBLIC_HERO_VIDEO_URL", "  https://cdn.test/hero.mp4  ");
    vi.resetModules();
    const mod = await import("@/components/hero-video");

    expect(mod.HERO_VIDEO_SRC).toBe("https://cdn.test/hero.mp4");
  });
});

// ── 组件行为 ────────────────────────────────────────────────────────────────

function stubMatchMedia({ reducedMotion }: { reducedMotion: boolean }): void {
  vi.stubGlobal(
    "matchMedia",
    vi.fn((query: string) => ({
      matches: reducedMotion,
      media: query,
      onchange: null,
      addEventListener: () => undefined,
      removeEventListener: () => undefined,
      addListener: () => undefined,
      removeListener: () => undefined,
      dispatchEvent: () => false,
    })) as unknown as typeof window.matchMedia,
  );
}

/** 手动驱动的 rAF：记录回调，由用例决定何时"跑一帧"。 */
function stubAnimationFrame(): {
  frames: FrameRequestCallback[];
  request: ReturnType<typeof vi.fn>;
  cancel: ReturnType<typeof vi.fn>;
} {
  const frames: FrameRequestCallback[] = [];
  const request = vi.fn((callback: FrameRequestCallback) => {
    frames.push(callback);
    return frames.length;
  });
  const cancel = vi.fn();
  vi.stubGlobal("requestAnimationFrame", request);
  vi.stubGlobal("cancelAnimationFrame", cancel);
  return { frames, request, cancel };
}

/** jsdom 里 duration / currentTime 是只读包装属性，直接定义成可控的数据属性。 */
function setMedia(
  video: HTMLVideoElement,
  duration: number,
  currentTime = 0,
): void {
  Object.defineProperty(video, "duration", { value: duration, configurable: true });
  Object.defineProperty(video, "currentTime", {
    value: currentTime,
    writable: true,
    configurable: true,
  });
}

function getVideo(container: HTMLElement): HTMLVideoElement {
  const video = container.querySelector("video");
  if (video === null) throw new Error("HeroVideo 没有渲染出 <video>");
  return video;
}

function runNextFrame(frames: FrameRequestCallback[]): void {
  const frame = frames.shift();
  if (frame === undefined) throw new Error("没有排队的 requestAnimationFrame");
  frame(0);
}

describe("HeroVideo", () => {
  let playMock: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    stubMatchMedia({ reducedMotion: false });
    // play() 在 jsdom 里是 not-implemented：用例里换掉它，既能断言调用次数，
    // 也避免它往 stderr 打警告。
    playMock = vi
      .spyOn(HTMLMediaElement.prototype, "play")
      .mockResolvedValue(undefined);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it("渲染的是「装饰性背景」：静音、行内播放、不预加载整段、不占焦点、句柄可控", () => {
    const { container } = render(<HeroVideo />);
    const video = getVideo(container);

    expect(video).toHaveAttribute("src", HERO_VIDEO_SRC);
    expect(video).toHaveAttribute("preload", "metadata");
    expect(video).toHaveAttribute("aria-hidden", "true");
    expect(video).toHaveAttribute("tabindex", "-1");
    expect(video.muted).toBe(true);
    expect(video.playsInline).toBe(true);
    // 起始完全透明：等元数据到了再由 RAF 淡入
    expect(video.style.opacity).toBe("0");

    // ★ 不用原生 loop，也不写 autoPlay：循环与播放时机都由本组件控制 ★
    expect(video).not.toHaveAttribute("loop");
    expect(video).not.toHaveAttribute("autoplay");
  });

  it("可以换成自己的视频资源（NEXT_PUBLIC_HERO_VIDEO_URL 的落点）", () => {
    const { container } = render(<HeroVideo src="https://example.test/own.mp4" />);
    expect(getVideo(container)).toHaveAttribute(
      "src",
      "https://example.test/own.mp4",
    );
  });

  it("★ 减少动效：不排任何 rAF、不调用 play()，背景保持静态", () => {
    stubMatchMedia({ reducedMotion: true });
    const { frames, request } = stubAnimationFrame();

    const { container } = render(<HeroVideo />);
    const video = getVideo(container);

    act(() => {
      video.dispatchEvent(new Event("loadedmetadata"));
    });

    expect(request).not.toHaveBeenCalled();
    expect(frames).toHaveLength(0);
    expect(playMock).not.toHaveBeenCalled();
    expect(video.style.opacity).toBe("0");
  });

  it("★ 元数据到达才开始播放，并按 currentTime 逐帧收敛透明度", () => {
    const { frames, request } = stubAnimationFrame();
    const { container } = render(<HeroVideo />);
    const video = getVideo(container);

    // 挂载阶段（jsdom 里 readyState = 0）不允许主动 play
    expect(request).not.toHaveBeenCalled();
    expect(playMock).not.toHaveBeenCalled();

    setMedia(video, 10);
    act(() => {
      video.dispatchEvent(new Event("loadedmetadata"));
    });
    expect(playMock).toHaveBeenCalledTimes(1);
    expect(frames).toHaveLength(1);

    const opacityAt = (currentTime: number) => {
      video.currentTime = currentTime;
      runNextFrame(frames);
      return video.style.opacity;
    };

    expect(opacityAt(0)).toBe("0");
    expect(opacityAt(HERO_VIDEO_FADE_S / 2)).toBe("0.5");
    expect(opacityAt(HERO_VIDEO_FADE_S)).toBe("1");
    expect(opacityAt(5)).toBe("1");
    expect(opacityAt(9.75)).toBe("0.5");
  });

  it("playing 事件负责（重新）启动逐帧循环，pause 事件停下它", () => {
    const { frames, request, cancel } = stubAnimationFrame();
    const { container } = render(<HeroVideo />);
    const video = getVideo(container);

    setMedia(video, 10);
    act(() => {
      video.dispatchEvent(new Event("playing"));
    });
    expect(request).toHaveBeenCalledTimes(1);

    // 已在跑的时候再来一次 playing 不会叠加出第二条循环
    act(() => {
      video.dispatchEvent(new Event("playing"));
    });
    expect(request).toHaveBeenCalledTimes(1);
    expect(frames).toHaveLength(1);

    act(() => {
      video.dispatchEvent(new Event("pause"));
    });
    expect(cancel).toHaveBeenCalledTimes(1);
  });

  it("★ 片尾：先黑场，等 100ms 回到起点再播一遍（无感循环）", () => {
    vi.useFakeTimers();
    const { frames, cancel } = stubAnimationFrame();
    const { container } = render(<HeroVideo />);
    const video = getVideo(container);

    setMedia(video, 10, 9.9);
    act(() => {
      video.dispatchEvent(new Event("loadedmetadata"));
    });
    expect(frames).toHaveLength(1);

    act(() => {
      video.dispatchEvent(new Event("ended"));
    });

    expect(cancel).toHaveBeenCalledTimes(1);
    expect(video.style.opacity).toBe("0");
    // 还没到 100ms：不重播
    expect(playMock).toHaveBeenCalledTimes(1);

    act(() => {
      vi.advanceTimersByTime(HERO_VIDEO_RESTART_DELAY_MS);
    });

    expect(video.currentTime).toBe(0);
    expect(playMock).toHaveBeenCalledTimes(2);
  });

  it("★ 浏览器拒绝自动播放时静默降级：不抛错、不留黑屏以外的副作用", async () => {
    const { frames } = stubAnimationFrame();
    playMock.mockRejectedValue(new Error("NotAllowedError"));

    const { container } = render(<HeroVideo />);
    const video = getVideo(container);

    setMedia(video, 10);
    await act(async () => {
      video.dispatchEvent(new Event("loadedmetadata"));
    });

    expect(frames).toHaveLength(1);
    expect(video.style.opacity).toBe("0");
  });

  it("play() 同步抛错（元素损坏 / 编解码不支持）也不会让首页崩掉", () => {
    stubAnimationFrame();
    playMock.mockImplementation(() => {
      throw new Error("no supported source");
    });

    const { container } = render(<HeroVideo />);
    const video = getVideo(container);

    setMedia(video, 10);
    act(() => {
      video.dispatchEvent(new Event("loadedmetadata"));
    });

    expect(video.style.opacity).toBe("0");
  });

  it("元数据已经就绪时（同页重挂载 / bfcache）不必再等事件", () => {
    const { request } = stubAnimationFrame();
    const { container, rerender } = render(<HeroVideo />);
    const video = getVideo(container);

    Object.defineProperty(video, "readyState", { value: 4, configurable: true });
    rerender(<HeroVideo src="https://example.test/next.mp4" />);

    expect(request).toHaveBeenCalledTimes(1);
    expect(playMock).toHaveBeenCalledTimes(1);
  });

  it("★ 卸载后不留定时器、不留监听；再来的事件不会触发任何播放", () => {
    vi.useFakeTimers();
    stubAnimationFrame();
    const { container, unmount } = render(<HeroVideo />);
    const video = getVideo(container);

    setMedia(video, 10, 9.9);
    act(() => {
      video.dispatchEvent(new Event("loadedmetadata"));
    });
    act(() => {
      video.dispatchEvent(new Event("ended"));
    });
    expect(vi.getTimerCount()).toBe(1);

    unmount();
    expect(vi.getTimerCount()).toBe(0);

    const callsBefore = playMock.mock.calls.length;
    video.dispatchEvent(new Event("ended"));
    act(() => {
      vi.advanceTimersByTime(HERO_VIDEO_RESTART_DELAY_MS * 5);
    });
    expect(playMock.mock.calls.length).toBe(callsBefore);
  });
});
