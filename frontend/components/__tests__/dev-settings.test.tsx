/**
 * 开发设置面板（`components/dev-settings.tsx`）的交互测试。
 *
 * 这个面板能写 `.env` 与 `config/*.yaml`，写错就是真的改坏本地环境，
 * 所以除"能不能用"之外，这里重点守四条诚实性规则：
 *
 * 1. **不做看起来成功的保存**：后端没确认的改动不许说"已保存"；
 * 2. **只提交改动过的键**：否则面板会把整份清单写回 `.env`；
 * 3. **Secret 留空 = 不修改**：留空不能被理解成清空；
 * 4. **校验失败要说清"文件没被动"**：否则用户会以为文件已经改了一半。
 */

import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { DevSettings } from "@/components/dev-settings";
import { ApiError, NetworkError } from "@/lib/api";
import type { DevConfig, DevEnvField } from "@/lib/dev-api";

const fetchDevConfig = vi.fn();
const updateDevEnv = vi.fn();
const updateDevConfigFile = vi.fn();
const readAdminToken = vi.fn();
const saveAdminToken = vi.fn();

vi.mock("@/lib/dev-api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/dev-api")>("@/lib/dev-api");
  return {
    ...actual,
    fetchDevConfig: (...args: unknown[]) => fetchDevConfig(...args),
    updateDevEnv: (...args: unknown[]) => updateDevEnv(...args),
    updateDevConfigFile: (...args: unknown[]) => updateDevConfigFile(...args),
    readAdminToken: () => readAdminToken(),
    saveAdminToken: (token: string) => saveAdminToken(token),
  };
});

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

const CONFIG: DevConfig = {
  env: [
    field({
      key: "LLM_PROVIDER",
      label: "LLM Provider",
      choices: ["deepseek", "null"],
      hint: "无 Key 时自动降级为规则引擎",
    }),
    field({
      key: "DEEPSEEK_API_KEY",
      label: "DeepSeek Key",
      group: "密钥",
      kind: "secret",
      is_set: true,
      value: "sk-••••1234",
      hint: "填上才会真正调用模型",
    }),
    // 没写 hint 的字段不该出现圈感叹号（一个空的提示比没有提示更差）
    field({
      key: "TAVILY_API_KEY",
      label: "Tavily Key",
      group: "密钥",
      kind: "secret",
      is_set: false,
      value: "",
    }),
  ],
  config_files: [
    { name: "pricing.yaml", content: "llm:\n  deepseek-flash:\n    input: 1\n" },
    { name: "routing.yaml", content: "routing:\n  walk_speed_kmh: 4.5\n" },
  ],
  effective: {
    llm_provider: "deepseek",
    env: "development",
    frontend_env: {
      keys: [
        {
          key: "NEXT_PUBLIC_AMAP_JS_KEY",
          note: "结果页的站点示意图（浏览器加载高德 JS API 用）",
          is_set: true,
          source: "frontend/.env.local",
        },
        {
          key: "AMAP_SECURITY_CODE",
          note: "高德安全密钥；只在服务端代理里补 jscode，不进前端产物",
          is_set: false,
          source: "",
        },
      ],
      files: ["frontend/.env.local", "frontend/.env"],
      editable_here: false,
      explanation: "这些变量写在 frontend/.env.local，Next.js 只读那里；本面板改的是仓库根的 .env。",
    },
  },
  notice: "开发模式：接口只在 ENV=development 时注册。",
};

beforeEach(() => {
  fetchDevConfig.mockReset();
  updateDevEnv.mockReset();
  updateDevConfigFile.mockReset();
  readAdminToken.mockReset();
  saveAdminToken.mockReset();

  readAdminToken.mockReturnValue("tok");
  fetchDevConfig.mockResolvedValue(CONFIG);
});

/** 渲染并等到配置加载完成（首次加载一定会 await fetchDevConfig）。 */
async function renderPanel() {
  render(<DevSettings />);
  await screen.findByRole("heading", { name: "环境变量" });
}

describe("开发设置面板", () => {
  it("加载后按后端分组展示环境变量，并明确写出这是开发工具", async () => {
    await renderPanel();

    expect(screen.getByText(/开发模式：接口只在 ENV=development 时注册/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "pricing.yaml" })).toBeInTheDocument();

    const llm = screen.getByLabelText(/LLM Provider/);
    expect(llm).toHaveValue("deepseek");

    // Secret 只显示打码值，且留空有明确语义
    const key = screen.getByLabelText(/DEEPSEEK_API_KEY/);
    expect(key).toHaveValue("");
    expect(key).toHaveAttribute("type", "password");
    expect(screen.getByPlaceholderText("（已配置，留空表示不修改）")).toBeInTheDocument();
    expect(screen.getByPlaceholderText("（未配置）")).toBeInTheDocument();
  });

  it("只提交真正改动过的项，不是整份清单", async () => {
    const user = userEvent.setup();
    await renderPanel();

    updateDevEnv.mockResolvedValue(["LLM_PROVIDER"]);
    await user.selectOptions(screen.getByLabelText(/LLM Provider/), "null");
    await user.click(screen.getByRole("button", { name: "保存环境变量" }));

    expect(updateDevEnv).toHaveBeenCalledWith("tok", { LLM_PROVIDER: "null" });
    expect(await screen.findByText(/已更新并立即生效：LLM_PROVIDER/)).toBeInTheDocument();
  });

  it("什么都没改时如实说「没有改动」，不发请求", async () => {
    const user = userEvent.setup();
    await renderPanel();

    await user.click(screen.getByRole("button", { name: "保存环境变量" }));

    expect(updateDevEnv).not.toHaveBeenCalled();
    expect(screen.getByText("没有改动需要保存。")).toBeInTheDocument();
  });

  it("Secret 填了新值才提交（留空不提交、不当作清空）", async () => {
    const user = userEvent.setup();
    await renderPanel();

    updateDevEnv.mockResolvedValue(["DEEPSEEK_API_KEY"]);
    await user.type(screen.getByLabelText(/DEEPSEEK_API_KEY/), "sk-new");
    await user.click(screen.getByRole("button", { name: "保存环境变量" }));

    expect(updateDevEnv).toHaveBeenCalledWith("tok", { DEEPSEEK_API_KEY: "sk-new" });
  });

  it("保存配置文件成功后回报字节数", async () => {
    const user = userEvent.setup();
    await renderPanel();

    updateDevConfigFile.mockResolvedValue({ name: "routing.yaml", bytes: 27 });
    await user.click(screen.getByRole("button", { name: "routing.yaml" }));
    await user.click(screen.getByRole("button", { name: "保存 routing.yaml" }));

    expect(updateDevConfigFile).toHaveBeenCalledWith(
      "tok",
      "routing.yaml",
      "routing:\n  walk_speed_kmh: 4.5\n",
    );
    expect(
      await screen.findByText(/routing.yaml 已保存（27 字节）并通过配置校验/),
    ).toBeInTheDocument();
  });

  it("校验失败时明确说「文件未被修改」，并给出 hint", async () => {
    const user = userEvent.setup();
    await renderPanel();

    updateDevConfigFile.mockRejectedValue(
      new ApiError(
        {
          code: "CONFIG_INVALID",
          message: "配置校验未通过",
          hint: "walk_speed_kmh 必须大于 0",
          context: {},
        },
        422,
        null,
      ),
    );
    await user.click(screen.getByRole("button", { name: "保存 pricing.yaml" }));

    const note = await screen.findByRole("status");
    expect(note).toHaveTextContent("配置校验未通过");
    expect(note).toHaveTextContent("walk_speed_kmh 必须大于 0");
    expect(note).toHaveTextContent("文件未被修改");
  });

  it("加载失败不呈现空面板，而是给出可读原因（并退回默认说明）", async () => {
    fetchDevConfig.mockRejectedValue(new NetworkError(new Error("offline")));
    render(<DevSettings />);

    const note = await screen.findByRole("status");
    expect(note).toHaveTextContent(/无法连接到规划服务/);
    expect(screen.queryByRole("heading", { name: "环境变量" })).not.toBeInTheDocument();
    // 后端连不上时拿不到 notice，页面要退回自己的说明而不是空着
    expect(screen.getByText(/这是开发期的配置面板，不是用户功能/)).toBeInTheDocument();
  });

  it("只有错误码、没有 message 时也能给出可读原因", async () => {
    fetchDevConfig.mockRejectedValue({ code: "DEV_DISABLED" });
    render(<DevSettings />);

    expect(await screen.findByRole("status")).toHaveTextContent("请求失败：DEV_DISABLED");
  });

  it("Secret 的清空是显式动作：勾选后提交空值，取消则不发", async () => {
    const user = userEvent.setup();
    await renderPanel();

    updateDevEnv.mockResolvedValue(["DEEPSEEK_API_KEY"]);
    await user.click(screen.getByRole("button", { name: "清空该项" }));
    expect(screen.getByText(/保存后该项会被清空/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "保存环境变量" }));
    expect(updateDevEnv).toHaveBeenCalledWith("tok", { DEEPSEEK_API_KEY: "" });

    // 取消清空后，未改动的 Secret 不再被提交（保存后的刷新会重置面板状态）
    updateDevEnv.mockClear();
    await user.click(screen.getByRole("button", { name: "清空该项" }));
    await user.click(screen.getByRole("button", { name: "取消清空" }));
    expect(screen.queryByText(/保存后该项会被清空/)).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "保存环境变量" }));
    expect(updateDevEnv).not.toHaveBeenCalled();
  });

  it("提交新 Token 会存起来并重新加载", async () => {
    const user = userEvent.setup();
    await renderPanel();
    expect(fetchDevConfig).toHaveBeenCalledWith("tok");

    const input = screen.getByLabelText(/ADMIN_TOKEN/);
    await user.clear(input);
    await user.type(input, "new-token");
    await user.click(screen.getByRole("button", { name: "加载配置" }));

    expect(saveAdminToken).toHaveBeenCalledWith("new-token");
    expect(fetchDevConfig).toHaveBeenLastCalledWith("new-token");
  });

  it("展示运行中进程的生效快照（改完没生效要对得上）", async () => {
    await renderPanel();

    const snapshot = screen.getByTestId("effective-config");
    expect(within(snapshot).getByText(/llm_provider/)).toBeInTheDocument();
  });

  it("★ 每个字段都有一个圈感叹号，说明就来自后端那句 hint（不另抄一份）", async () => {
    await renderPanel();

    const trigger = screen.getByTestId("tip-trigger-LLM_PROVIDER");
    const tooltipId = trigger.getAttribute("aria-describedby");
    expect(tooltipId).toBe("tip-LLM_PROVIDER");

    // 说明常驻在 DOM 里（不是只在悬停时才插进去）：键盘、屏幕阅读器、Ctrl+F 都摸得到
    const tooltip = document.getElementById("tip-LLM_PROVIDER");
    expect(tooltip).toHaveAttribute("role", "tooltip");
    expect(tooltip).toHaveTextContent("无 Key 时自动降级为规则引擎");

    // ★ 控件自己 `aria-describedby` 到同一段说明：字段被读到时说明就在，
    // 不要求用户先摸到那个圈感叹号（也不是只在悬停时才出现）
    expect(screen.getByLabelText(/LLM Provider/)).toHaveAttribute(
      "aria-describedby",
      "tip-LLM_PROVIDER",
    );
    expect(screen.getByLabelText(/DEEPSEEK_API_KEY/)).toHaveAttribute(
      "aria-describedby",
      "tip-DEEPSEEK_API_KEY",
    );
    // 没有说明的字段就不会被描述（而不是指向一个不存在的 id）
    expect(screen.getByLabelText(/TAVILY_API_KEY/)).not.toHaveAttribute("aria-describedby");

    // 没有说明的字段不该出现一个空提示
    expect(screen.queryByTestId("tip-trigger-TAVILY_API_KEY")).toBeNull();
  });

  it("圈感叹号点一下能开合（触屏没有悬停，只能靠点击）", async () => {
    const user = userEvent.setup();
    await renderPanel();

    const trigger = screen.getByTestId("tip-trigger-DEEPSEEK_API_KEY");
    expect(trigger).toHaveAttribute("aria-expanded", "false");

    await user.click(trigger);
    expect(trigger).toHaveAttribute("aria-expanded", "true");

    await user.click(trigger);
    expect(trigger).toHaveAttribute("aria-expanded", "false");
  });

  it("点击圈感叹号不会顺手改动字段值（它是说明，不是控件）", async () => {
    const user = userEvent.setup();
    await renderPanel();

    await user.click(screen.getByTestId("tip-trigger-LLM_PROVIDER"));
    await user.click(screen.getByRole("button", { name: "保存环境变量" }));

    expect(updateDevEnv).not.toHaveBeenCalled();
  });

  it("★ 前端专用配置单独一栏：说清「本面板改不了」与各自配没配", async () => {
    await renderPanel();

    // 这一栏存在的理由：面板上「高德 Web 服务 Key（未配置）」不该被读成「地图整体没配」
    const heading = screen.getByRole("heading", { name: "前端专用配置（在这里只读）" });
    // 限定在这一张卡里查：生效快照那段 JSON 里也嵌着同一个 explanation
    const card = within(heading.parentElement as HTMLElement);
    expect(card.getByText(/Next\.js 只读那里/)).toBeInTheDocument();

    expect(card.getByText("NEXT_PUBLIC_AMAP_JS_KEY")).toBeInTheDocument();
    expect(card.getByText("已配置 · frontend/.env.local")).toBeInTheDocument();
    expect(card.getByText("未配置")).toBeInTheDocument();
    expect(card.getByText(/要改这些变量：直接编辑/)).toBeInTheDocument();

    // 只读：这里没有输入框，也没有保存按钮
    expect(screen.queryByLabelText(/NEXT_PUBLIC_AMAP_JS_KEY/)).toBeNull();
  });

  it("后端没给前端配置状态时整块不渲染（而不是画一张空表）", async () => {
    fetchDevConfig.mockResolvedValue({ ...CONFIG, effective: { env: "development" } });
    await renderPanel();

    expect(
      screen.queryByRole("heading", { name: "前端专用配置（在这里只读）" }),
    ).toBeNull();
    // 其余部分照常呈现：少一块不代表整页坏掉
    expect(screen.getByRole("heading", { name: "环境变量" })).toBeInTheDocument();
  });
});
