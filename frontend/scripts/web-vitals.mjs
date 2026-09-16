#!/usr/bin/env node
/**
 * 用 Playwright + CDP 量 LCP（PRD §23.6 的"分享页 LCP ≤ 2.5s（4G）"与首页 TT­I 那一行）。
 *
 * ★ 为什么不直接用 Lighthouse CLI ★
 * Lighthouse 要额外装一份浏览器与一大包依赖（约百 MB），而这个仓库已经有 Playwright
 * 和它自带的 Chrome。LCP 本身是一个 PerformanceObserver 指标，用 CDP 把网络与 CPU
 * 压到 4G/4x 就能拿到同一口径的数字，换来的是"不用为了一行指标装第二个浏览器"。
 * **代价要说清楚**：这不是 Lighthouse 分数，没有它的可访问性/SEO 检查，
 * 也没有它对 LCP 的像素级候选回溯。所以报告里只把它当 LCP 读数用，不冒充 Lighthouse 审计。
 *
 * ★ TTI 刻意不测 ★
 * "可交互时间"的正确定义需要长任务分析（TTI = 首个 5s 静默窗之前最后一个长任务结束）。
 * 用 domInteractive 之类的东西顶替会得到一个偏低且含义不同的数字 —— 宁可写"未测"。
 *
 * 用法（一般由 `make perf-lcp` / `frontend/scripts/perf-lcp.sh` 调用：
 * 那个脚本负责把前后端起起来并造一条真实的分享行程，本脚本只管量）：
 *   node scripts/web-vitals.mjs --base http://127.0.0.1:3100 --slug abc123 --out ../.perf/web-vitals.json
 *
 * `--slug` 缺省时**只量首页**，不编造分享页的数字（报告里那一项会写成未测）。
 */

import { writeFileSync, mkdirSync } from "node:fs";
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
 * 取 LCP：挂在 PerformanceObserver 上，等 load 之后再给 2s 让迟到的候选进来。
 * LargestContentfulPaint 的 entries 会随渲染更新，取最后一个才是最终值。
 */
async function measureLcp(browser, url) {
  const context = await browser.newContext({ viewport: { width: 412, height: 915 }, deviceScaleFactor: 2 });
  const page = await context.newPage();
  const client = await context.newCDPSession(page);
  await client.send("Network.emulateNetworkConditions", { offline: false, ...NETWORK });
  await client.send("Emulation.setCPUThrottlingRate", { rate: CPU_RATE });

  await page.addInitScript(() => {
    window.__lcp = 0;
    new PerformanceObserver((list) => {
      for (const entry of list.getEntries()) window.__lcp = entry.startTime;
    }).observe({ type: "largest-contentful-paint", buffered: true });
  });

  const started = Date.now();
  await page.goto(url, { waitUntil: "load", timeout: 60_000 });
  await page.waitForTimeout(2_000);
  const lcp = await page.evaluate(() => window.__lcp ?? 0);
  const navigation = await page.evaluate(() => {
    const entry = performance.getEntriesByType("navigation")[0];
    return entry
      ? { dom_content_loaded_ms: Math.round(entry.domContentLoadedEventEnd), load_ms: Math.round(entry.loadEventEnd) }
      : null;
  });
  await context.close();
  return { lcp_ms: Math.round(lcp), navigation, wall_ms: Date.now() - started };
}

function median(values) {
  const sorted = [...values].sort((a, b) => a - b);
  return sorted[Math.floor(sorted.length / 2)];
}

const browser = await chromium.launch();
const targets = [{ name: "home", url: `${base}/` }];
if (slug) targets.push({ name: "share", url: `${base}/t/${slug}` });

const result = { base, cpu_throttle: CPU_RATE, network: "4G (4Mbps/3Mbps/100ms RTT，移动端 viewport)", pages: {} };
for (const target of targets) {
  const samples = [];
  for (let i = 0; i < runs; i += 1) samples.push((await measureLcp(browser, target.url)).lcp_ms);
  result.pages[target.name] = { url: target.url, lcp_samples_ms: samples, lcp_median_ms: median(samples) };
}
await browser.close();

mkdirSync(dirname(resolve(out)), { recursive: true });
writeFileSync(resolve(out), JSON.stringify(result, null, 1), "utf8");
console.log(JSON.stringify(result, null, 1));
