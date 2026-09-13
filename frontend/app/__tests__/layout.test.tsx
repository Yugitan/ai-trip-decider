/**
 * 根布局（`app/layout.tsx`）的测试。
 *
 * 为什么值得测一个"看起来只是壳"的文件：
 * 1. 它此前是覆盖率里唯一 0% 的源文件（`layout.tsx 0 | 3-39`）—— 一个从未被执行过的
 *    文件放在统计口径内，会让"覆盖率"这件事本身失真。
 * 2. 它承载的不是纯样式，而是**对外的可发现性配置**：`lang="zh-CN"` 决定屏幕阅读器
 *    用哪种语音朗读、`title.template` 决定每个子页面在搜索结果里的样子、
 *    `maximumScale: 5` 是无障碍要求（不得禁止用户放大）。
 *    这些值改错了不会让任何页面报错，只会安静地变差 —— 正是最该被钉住的一类。
 *
 * 写法说明：这里**直接调用**组件函数取它的返回元素，而不是用 testing-library 渲染。
 * 因为根布局的最外层就是 `<html>`，把它塞进 RTL 默认的 `<div>` 容器会触发
 * React 的 "In HTML, <html> cannot be a child of <div>" 警告 —— 测试照样通过，
 * 但会在 CI 日志里留下一条刺眼的 stderr，长期看会训练人忽略警告。
 * RootLayout 没有 hooks，直接调用是安全的（纯函数）。
 */

import type { ReactElement } from "react";
import { describe, expect, it } from "vitest";

import RootLayout, { metadata, viewport } from "@/app/layout";

type HtmlProps = { lang?: string; children?: ReactElement<{ className?: string }> };

function renderLayoutElement() {
  const element = RootLayout({ children: <p>页面内容</p> }) as ReactElement<HtmlProps>;
  // 非空断言前先确认形状，避免下面报错时看不出原因
  expect(typeof element.type).toBe("string");
  return element;
}

describe("RootLayout", () => {
  it("最外层是 <html>，并把文档语言标成 zh-CN", () => {
    const element = renderLayoutElement();

    expect(element.type).toBe("html");
    // 语言标签写错会让读屏软件用英文语音念中文
    expect(element.props.lang).toBe("zh-CN");
  });

  it("children 被包在 <body> 里，且 body 带最小高度与抗锯齿类名（不是空壳）", () => {
    const element = renderLayoutElement();
    const body = element.props.children;

    expect(body?.type).toBe("body");
    expect(body?.props.className).toContain("min-h-dvh");
    expect(body?.props.className).toContain("antialiased");
  });
});

describe("根布局导出的元信息", () => {
  it("title 是模板形式，子页面标题会自动接上站点名", () => {
    const title = metadata.title;
    // 模板形式（对象）而非字符串：否则 /explore 之类的子页面标题会被整条覆盖
    expect(title).toMatchObject({ template: expect.stringContaining("TripDecider") });
  });

  it("提供 description 与 openGraph，避免分享出去是空白卡片", () => {
    expect(metadata.description).toBeTruthy();
    expect(metadata.openGraph?.title).toBeTruthy();
    expect(metadata.openGraph?.description).toBeTruthy();
  });

  it("允许用户放大到 5 倍（无障碍硬性要求，禁止写死 maximumScale: 1）", () => {
    expect(viewport.width).toBe("device-width");
    expect(viewport.initialScale).toBe(1);
    expect(viewport.maximumScale).toBe(5);
  });
});
