#!/usr/bin/env node
/**
 * 用 Playwright + CDP 采集首屏指标（PRD §23.6 的「分享页 LCP ≤ 2.5s」与「首页 TTI ≤ 3s」）。
 *
 * ★ 本文件只负责**采集**，不下结论 ★
 * LCP 是「时间轴上最后一个候选」；TTI 需要长任务 + 静默窗，那套判定逻辑在
 * `backend/scripts/benchmark.py::compute_tti_ms`（纯函数，有单测）。
 * 把判定规则放在能写测试的地方，顺便保证口径只有一份 —— 本文件改一次
 * 采集方式，不会悄悄改掉「什么算达标」（反之亦然）。
 *
 * ★ 为什么不直接用 Lighthouse CLI ★
 * Lighthouse 要额外装一份浏览器与一大包依赖（约百 MB），而这个仓库已经有 Playwright
 * 和它自带的 Chrome。LCP/TTI 本身都是 PerformanceObserver 指标，用 CDP 把网络与 CPU
 * 压到 4G/4x 就能拿到同一口径的数字，换来的是「不用为了一行指标装第二个浏览器」。
 * **代价要说清楚**：这不是 Lighthouse 分数，没有它的可访问性/SEO 检查，
 * 也没有它对 LCP 的像素级候选回溯。所以报告里只把它当读数用，不冒充 Lighthouse 审计。
 *
 * ★ TTI 的两个前提都要采集 ★
 * Lighthouse 的 TTI：FCP 之后第一个「5s 内没有长任务、且**在途请求 ≤ 2**」的窗口之前，
 * 最后一个长任务的结束时刻。所以这里既收长任务（`longtask`），也收请求的在途区间
 * （Resource Timing 的 `startTime → responseEnd`）。
 * **少了在途请求这一条会得到一个系统性偏乐观的数**：页面刚画完首帧、脚本还没跑的那一瞬间
 * 主线程往往是安静的，于是 TTI 会被判成 FCP。两半都收，Python 侧才判得准。
 * 找不到完整静默窗时**不报数**（`observed_until_ms` 会说明观测到哪儿了），
 * 宁可不报，也不拿「最后一个长任务」顶上冒充。
 *
 * 用法（一般由 `make perf-lcp` / `frontend/scripts/perf-lcp.sh` 调用：
 * 那个脚本负责把前后端起起来并造一条真实的分享行程，本脚本只管量）：
 *   node scripts/web-vitals.mjs --base http://127.0.0.1:3100 --slug abc123 --out ../.perf/web-vitals.json
 *
 * `--slug` 缺省时**只量首页**，不编造分享页的数字（报告里那一项会写成未测）。
 */

import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import process from "node:process";
// 从 `@playwright/test` 取 chromium：pnpm 只把**直接依赖**暴露在 node_modules 下，
// `playwright-core` 是间接依赖，直接 import 会 ERR_MODULE_NOT_FOUND。
import { chromium } from "@playwright/test";

const args = new Map();
for (let i = 2; i < process.argv.length; i += 2) {
  const key = process.argv[i].replace(/^--/, "");
  args.set(key, process.argv[i + 1]);
}

const base = args.get("base") ?? "http://127.0.0.1:3100";
const slug = args.get("slug") ?? "";
const out = args.get("out") ?? "../.perf/web-vitals.json";
const runs = Number(args.get("runs") ?? 3);

/** 4G：下行 4Mbps / 上行 3Mbps / RTT 100ms；CPU 4 倍降速（Lighthouse 移动端的常用档位）。 */
const NETWORK = { downloadThroughput: (4 * 1024 * 1024) / 8, uploadThroughput: (3 * 1024 * 1024) / 8, latency: 100 };
const CPU_RATE = 4;

/**
 * TTI 的静默窗：这么长时间里一个长任务都没有，才认为「用户能用了」。
 * 这个值会写进 JSON 交给 Python 用；两边不各写一份。
 */
const QUIET_WINDOW_MS = 5000;
/** 轮询间隔与「一直不静默就别无限等」的上限（从 load 之后开始算）。 */
const QUIET_POLL_MS = 250;
const QUIET_DEADLINE_MS = 20_000;

/**
 * 等到「主线程和网络都安静下来了」为止，并把「观测到哪儿」如实带回去。
 *
 * ★ 两个条件都要等，不能只看长任务 ★
 * 实测踩过：首页的长任务 0.7s 就没了，而资源到 3.5s 才陆续结束 —— 只等长任务的话，
 * 采集在 5.8s 就收工，而真正合格的那个静默窗是 [3.5s, 8.5s]，于是 TTI 只能报"未测"。
 * 这里等的是：最近一个长任务结束 ≥ 5s **且** 最近一个资源结束 ≥ 5s。
 *
 * ⚠︎ 已知差异：在途区间只能来自**已完成**的资源条目，所以"一直挂着不结束的请求"
 * （比如持续下载的媒体）在采集结束时是看不见的。采集在最后一项结束之后再等 5s，
 * 这个窗口足够覆盖我们页面上的常规资源；真要严格对齐 Lighthouse 得用 trace。
 *
 * 返回 `quiet: false` **不是失败**，而是"这段时间里静默窗没出现"——Python 侧会据此
 * 把 TTI 记为未测，而不是拿最后一个长任务顶上。
 */
async function waitForQuietWindow(page) {
  const startedAt = Date.now();
  for (;;) {
    const state = await page.evaluate(() => ({
      now: performance.now(),
      lastTaskEnd: window.__longTasks.reduce((max, task) => Math.max(max, task.start + task.duration), 0),
      lastResourceEnd: window.__resources.reduce((max, resource) => Math.max(max, resource.end), 0),
    }));
    const idleSince = Math.max(state.lastTaskEnd, state.lastResourceEnd);
    if (state.now - idleSince >= QUIET_WINDOW_MS) {
      return { quiet: true, observed_until_ms: state.now };
    }
    if (Date.now() - startedAt >= QUIET_DEADLINE_MS) {
      return { quiet: false, observed_until_ms: state.now };
    }
    await page.waitForTimeout(QUIET_POLL_MS);
  }
}

async function measurePage(browser, url) {
  const context = await browser.newContext({ viewport: { width: 412, height: 915 }, deviceScaleFactor: 2 });
  const page = await context.newPage();
  const client = await context.newCDPSession(page);
  await client.send("Network.emulateNetworkConditions", { offline: false, ...NETWORK });
  await client.send("Emulation.setCPUThrottlingRate", { rate: CPU_RATE });

  await page.addInitScript(() => {
    window.__lcp = 0;
    window.__longTasks = [];
    // LargestContentfulPaint 的 entries 会随渲染更新，取最后一个才是最终值。
    new PerformanceObserver((list) => {
      for (const entry of list.getEntries()) window.__lcp = entry.startTime;
    }).observe({ type: "largest-contentful-paint", buffered: true });
    // 长任务（Long Tasks API：>50ms 的阻塞任务）—— TTI 的输入之一。
    // 在 document 创建时就挂上，所以不需要 buffered 也漏不掉。
    new PerformanceObserver((list) => {
      for (const entry of list.getEntries()) {
        window.__longTasks.push({ start: entry.startTime, duration: entry.duration });
      }
    }).observe({ type: "longtask", buffered: true });
    // 每个资源的「在途」区间 —— TTI 的另一个输入（静默窗内不允许超过 2 个请求在飞）。
    // responseEnd 为 0（拿不到、或缓存命中）的条目丢掉：它们形不成有效区间。
    window.__resources = [];
    new PerformanceObserver((list) => {
      for (const entry of list.getEntries()) {
        if (entry.responseEnd >= entry.startTime) {
          window.__resources.push({ start: entry.startTime, end: entry.responseEnd });
        }
      }
    }).observe({ type: "resource", buffered: true });
  });

  const started = Date.now();
  await page.goto(url, { waitUntil: "load", timeout: 60_000 });
  const observed = await waitForQuietWindow(page);

  const data = await page.evaluate(() => {
    const fcp = performance.getEntriesByName("first-contentful-paint")[0];
    const navigation = performance.getEntriesByType("navigation")[0];
    return {
      lcp_ms: Math.round(window.__lcp ?? 0),
      fcp_ms: fcp ? Math.round(fcp.startTime) : null,
      long_tasks: window.__longTasks.map((task) => ({
        start: Math.round(task.start),
        duration: Math.round(task.duration),
      })),
      resources: window.__resources.map((resource) => ({
        start: Math.round(resource.start),
        end: Math.round(resource.end),
      })),
      navigation: navigation
        ? {
            dom_content_loaded_ms: Math.round(navigation.domContentLoadedEventEnd),
            load_ms: Math.round(navigation.loadEventEnd),
          }
        : null,
    };
  });

  await context.close();
  return {
    ...data,
    observed_until_ms: Math.round(observed.observed_until_ms),
    quiet_observed: observed.quiet,
    wall_ms: Date.now() - started,
  };
}

/** LCP/长任务都取中位数：单次读数会被 GC 或后端抖动带偏，与报告口径一致（取 3 次中位数）。 */
function median(values) {
  const sorted = [...values].sort((a, b) => a - b);
  return sorted[Math.floor(sorted.length / 2)];
}

const browser = await chromium.launch();
const targets = [{ name: "home", url: `${base}/` }];
if (slug) targets.push({ name: "share", url: `${base}/t/${slug}` });

const result = {
  base,
  cpu_throttle: CPU_RATE,
  network: "4G (4Mbps/3Mbps/100ms RTT，移动端 viewport)",
  quiet_window_ms: QUIET_WINDOW_MS,
  pages: {},
};
for (const target of targets) {
  const collected = [];
  for (let i = 0; i < runs; i += 1) collected.push(await measurePage(browser, target.url));
  const lcpSamples = collected.map((run) => run.lcp_ms);
  result.pages[target.name] = {
    url: target.url,
    lcp_samples_ms: lcpSamples,
    lcp_median_ms: median(lcpSamples),
    // 每次运行的原始证据：LCP/TTI 的判定都基于它们，报告里那句「n=几」也来自这里。
    runs: collected,
  };
}
await browser.close();

mkdirSync(dirname(resolve(out)), { recursive: true });
writeFileSync(resolve(out), JSON.stringify(result, null, 1), "utf8");
console.log(JSON.stringify(result, null, 1));
