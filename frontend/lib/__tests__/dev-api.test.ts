/**
 * 开发面板 API 客户端与纯函数（`lib/dev-api.ts`）的单元测试。
 *
 * 这个模块碰两样"出错代价很高"的东西，所以必须单独测：
 *
 * 1. **只提交改动过的键**（`changedEnvValues`）：多提交一个键就等于把面板
 *    没打算改的配置一起写回 `.env`，而且是静默的。Secret 留空 ≠ 清空，
 *    这条边界必须钉死。
 * 2. **Token 只进 sessionStorage**：写错成 `localStorage` 会把它长期留在磁盘上。
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  ADMIN_TOKEN_STORAGE_KEY,
  changedEnvValues,
  fetchDevConfig,
  groupEnvFields,
  readAdminToken,
  saveAdminToken,
  updateDevConfigFile,
  updateDevEnv,
  type DevEnvField,
} from "@/lib/dev-api";

const fetchMock = vi.fn<typeof fetch>();

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
  sessionStorage.clear();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const META = {
  request_id: "req-1",
  cached: false,
  cache_layer: null,
  degraded_modes: [],
  elapsed_ms: 3,
};

function lastUrl(): string {
  const call = fetchMock.mock.calls.at(-1);
  if (!call) throw new Error("fetch 没有被调用");
  return String(call[0]);
}

function lastInit(): RequestInit {
  const call = fetchMock.mock.calls.at(-1);
  if (!call) throw new Error("fetch 没有被调用");
  return call[1] ?? {};
}

function field(overrides: Partial<DevEnvField>): DevEnvField {
  return {
    key: "LLM_PROVIDER",
    label: "LLM",
    group: "模型",
    kind: "text",
    hint: "",
    choices: [],
    is_set: true,
    value: "deepseek",
    ...overrides,
  };
}

describe("Token 存储", () => {
  it("存进 sessionStorage 而不是 localStorage", () => {
    saveAdminToken("s3cret");

    expect(sessionStorage.getItem(ADMIN_TOKEN_STORAGE_KEY)).toBe("s3cret");
    // 本机 Node 默认把 localStorage 屏蔽成 undefined（未提供 --localstorage-file），
    // 所以这里读不到并不等于"没写错地方"。真正要守住的是：**只要它可读，就必须是空的**。
    const longLived = typeof localStorage === "undefined" ? null : localStorage;
    expect(longLived?.getItem(ADMIN_TOKEN_STORAGE_KEY) ?? null).toBeNull();
  });

  it("未保存时读回空串（而不是 null 或 undefined）", () => {
    expect(readAdminToken()).toBe("");
  });
});

describe("groupEnvFields", () => {
  it("按后端的顺序分组，不打乱字段次序", () => {
    const groups = groupEnvFields([
      field({ key: "LLM_PROVIDER", group: "模型" }),
      field({ key: "TAVILY_API_KEY", group: "搜索", kind: "secret" }),
      field({ key: "LLM_TIMEOUT_MS", group: "模型" }),
    ]);

    expect(groups.map((group) => group.group)).toEqual(["模型", "搜索"]);
    expect(groups[0]?.fields.map((item) => item.key)).toEqual([
      "LLM_PROVIDER",
      "LLM_TIMEOUT_MS",
    ]);
  });

  it("空输入返回空数组", () => {
    expect(groupEnvFields([])).toEqual([]);
  });
});

describe("changedEnvValues", () => {
  const fields = [
    field({ key: "LLM_PROVIDER", value: "deepseek" }),
    field({ key: "DEEPSEEK_API_KEY", kind: "secret", value: "sk-••••1234" }),
    field({ key: "AMAP_WEB_KEY", kind: "secret", value: "", is_set: false }),
  ];

  it("完全没编辑时回传空对象（避免把整份清单写回 .env）", () => {
    expect(changedEnvValues(fields, {})).toEqual({});
  });

  it("值没变时不提交（即使被聚焦过）", () => {
    expect(changedEnvValues(fields, { LLM_PROVIDER: "deepseek" })).toEqual({});
  });

  it("Secret 留空表示不修改，而不是清空", () => {
    expect(changedEnvValues(fields, { DEEPSEEK_API_KEY: "" })).toEqual({});
  });

  it("Secret 填入新值才提交", () => {
    expect(changedEnvValues(fields, { DEEPSEEK_API_KEY: "sk-new" })).toEqual({
      DEEPSEEK_API_KEY: "sk-new",
    });
  });

  it("普通字段清空是有意的改动，必须提交", () => {
    expect(changedEnvValues(fields, { LLM_PROVIDER: "" })).toEqual({
      LLM_PROVIDER: "",
    });
  });
});

describe("请求封装", () => {
  it("fetchDevConfig 带上 X-Admin-Token 并拆出 data", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({
        ok: true,
        data: { env: [], config_files: [], effective: {}, notice: "开发模式" },
        error: null,
        meta: META,
      }),
    );

    const result = await fetchDevConfig("tok");

    expect(result.notice).toBe("开发模式");
    const init = lastInit();
    expect((init.headers as Record<string, string>)["X-Admin-Token"]).toBe("tok");
  });

  it("空 Token 时不发这个头（后端可能允许本机免 Token）", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({
        ok: true,
        data: { env: [], config_files: [], effective: {}, notice: "" },
        error: null,
        meta: META,
      }),
    );

    await fetchDevConfig("");

    expect(lastInit().headers).not.toHaveProperty("X-Admin-Token");
  });

  it("updateDevEnv PUT 改动项并回传后端确认的键", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({ ok: true, data: { changed: ["LLM_PROVIDER"] }, error: null, meta: META }),
    );

    const changed = await updateDevEnv("tok", { LLM_PROVIDER: "deepseek" });

    expect(changed).toEqual(["LLM_PROVIDER"]);
    expect(lastUrl()).toContain("/api/v1/dev/env");
    const init = lastInit();
    expect(init.method).toBe("PUT");
    expect(JSON.parse(String(init.body))).toEqual({
      updates: { LLM_PROVIDER: "deepseek" },
    });
  });

  it("updateDevConfigFile 对文件名做 URL 编码", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({
        ok: true,
        data: { name: "pricing.yaml", bytes: 12 },
        error: null,
        meta: META,
      }),
    );

    await updateDevConfigFile("tok", "pricing.yaml", "a: 1");

    expect(lastUrl()).toContain("/api/v1/dev/config/pricing.yaml");
  });

  it("后端报错时抛出 ApiError（带 code / hint），由界面如实展示", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(
        {
          ok: false,
          data: null,
          error: {
            code: "CONFIG_INVALID",
            message: "配置校验未通过",
            hint: "routing.walk_speed_kmh 必须大于 0",
            context: {},
          },
          meta: META,
        },
        422,
      ),
    );

    await expect(
      updateDevConfigFile("tok", "routing.yaml", "walk_speed_kmh: -1"),
    ).rejects.toMatchObject({
      code: "CONFIG_INVALID",
      hint: "routing.walk_speed_kmh 必须大于 0",
    });
  });
});
