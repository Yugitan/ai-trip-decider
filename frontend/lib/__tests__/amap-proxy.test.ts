import { describe, expect, it } from "vitest";

import {
  amapUpstreamFor,
  buildUpstreamUrl,
  SECURITY_CODE_PARAM,
} from "@/lib/amap-proxy";

/**
 * 代理转发逻辑的测试（网络那一半在 route handler 里，这里只测 URL 怎么拼）。
 *
 * 最要紧的一条是 **jscode 必须被服务端的值覆盖**：
 * 如果允许调用方自带的 `jscode` 生效，任何人手拼一个
 * `/_AMapService/v3/geocode/geo?jscode=xxx` 就能拿我们的同源代理当跳板，
 * 而"用哪个密钥"这件事也就重新回到了浏览器手里 —— 代理层等于白设。
 */

describe("amapUpstreamFor", () => {
  it("样式接口走 webapi 上游（官方 nginx 示例里也是单独一条 location）", () => {
    expect(amapUpstreamFor(["v4", "map", "styles"])).toBe(
      "https://webapi.amap.com",
    );
    expect(amapUpstreamFor(["v4", "map", "styles", "preview"])).toBe(
      "https://webapi.amap.com",
    );
  });

  it("其余接口走 restapi 上游", () => {
    expect(amapUpstreamFor(["v3", "geocode", "geo"])).toBe(
      "https://restapi.amap.com",
    );
    expect(amapUpstreamFor(["v4", "map", "other"])).toBe(
      "https://restapi.amap.com",
    );
  });
});

describe("buildUpstreamUrl", () => {
  it("保留调用方的参数并补上 jscode", () => {
    const url = new URL(
      buildUpstreamUrl(
        ["v3", "geocode", "geo"],
        "?key=js-key&address=%E5%B9%BF%E5%B7%9E%E5%A1%94",
        "security-code",
      ),
    );
    expect(url.origin + url.pathname).toBe(
      "https://restapi.amap.com/v3/geocode/geo",
    );
    expect(url.searchParams.get("key")).toBe("js-key");
    expect(url.searchParams.get("address")).toBe("广州塔");
    expect(url.searchParams.get(SECURITY_CODE_PARAM)).toBe("security-code");
  });

  it("★ 调用方自带的 jscode 一律被覆盖（安全密钥只能由服务端决定）★", () => {
    const url = new URL(
      buildUpstreamUrl(
        ["v3", "geocode", "geo"],
        "?jscode=attacker-value&key=js-key",
        "server-value",
      ),
    );
    expect(url.searchParams.get(SECURITY_CODE_PARAM)).toBe("server-value");
    expect(url.searchParams.getAll(SECURITY_CODE_PARAM)).toEqual([
      "server-value",
    ]);
  });

  it("路径段逐段编码：段里的斜杠不会被当成新的路径分隔符", () => {
    const url = new URL(buildUpstreamUrl(["v3", "a/b"], "", "code"));
    expect(url.pathname).toBe("/v3/a%2Fb");
  });

  it("没有查询参数时也仍然带上 jscode", () => {
    const url = new URL(buildUpstreamUrl(["v3", "ip"], "", "code"));
    expect(url.searchParams.get(SECURITY_CODE_PARAM)).toBe("code");
  });
});
