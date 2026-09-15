/**
 * 开发设置面板的 API 客户端（**不是用户功能**，见后端 `app/api/v1/dev.py`）。
 *
 * 几条纪律：
 * 1. 复用 `lib/api.ts` 的 `request`：envelope 解析与错误映射只有一份，
 *    面板多一套 fetch 就意味着两套错误语义慢慢漂移；
 * 2. **Token 只存在 sessionStorage**：关掉标签页就没了，也不会被别的站点读到
 *    （`localStorage` 会长期留在磁盘上，对一个开发工具没必要）；
 * 3. 面板拿不到明文 Key：后端只回打码值，因此这里也**没有**"读出 Key"的路径。
 */

import { request } from "@/lib/api";

export interface DevEnvField {
  key: string;
  label: string;
  group: string;
  kind: string;
  hint: string;
  choices: string[];
  is_set: boolean;
  value: string;
}

export interface DevConfigFile {
  name: string;
  content: string;
}

export interface DevConfig {
  env: DevEnvField[];
  config_files: DevConfigFile[];
  effective: Record<string, unknown>;
  notice: string;
}

/** 一个**本面板改不了**的前端变量（写在 `frontend/.env.local`，Next 只读那里）。
 *
 * 后端只回报「配没配」与「从哪个文件读到的」，**永远没有值** ——
 * 与 Secret 的只写不读是同一条纪律。
 */
export interface DevFrontendEnvKey {
  key: string;
  note: string;
  is_set: boolean;
  source: string;
}

export interface DevFrontendEnv {
  keys: DevFrontendEnvKey[];
  files: string[];
  editable_here: boolean;
  explanation: string;
}

export interface DevConfigGroup {
  group: string;
  fields: DevEnvField[];
}

export const ADMIN_TOKEN_STORAGE_KEY = "tripdecider.dev.admin_token";

function storage(): Storage | null {
  try {
    return typeof sessionStorage === "undefined" ? null : sessionStorage;
  } catch {
    // 隐私模式下访问 sessionStorage 会抛错：面板不该因此整页崩掉
    return null;
  }
}

export function readAdminToken(): string {
  return storage()?.getItem(ADMIN_TOKEN_STORAGE_KEY) ?? "";
}

export function saveAdminToken(token: string): void {
  storage()?.setItem(ADMIN_TOKEN_STORAGE_KEY, token);
}

/** 按后端给的 group 分组并保持原顺序（后端的顺序就是"想让人先看到什么"的顺序）。 */
export function groupEnvFields(fields: DevEnvField[]): DevConfigGroup[] {
  const groups: DevConfigGroup[] = [];
  const byName = new Map<string, DevConfigGroup>();
  for (const field of fields) {
    const existing = byName.get(field.group);
    if (existing) {
      existing.fields.push(field);
      continue;
    }
    const created: DevConfigGroup = { group: field.group, fields: [field] };
    byName.set(field.group, created);
    groups.push(created);
  }
  return groups;
}

/** 只回传真正改动过的值：面板不该把整份清单都写一遍。
 *
 * ★ 为什么 Secret 需要单独的 `cleared` ★
 * Secret 输入框里显示的永远不是真值（后端只回打码值），因此"空字符串"有两种可能：
 * 「用户没碰过这个框」与「用户想把它清空」。两者无法从值本身区分，
 * 所以清空必须是**显式动作**（界面上的「清空该项」按钮），不能靠留空表达。
 * 反之，留空一律当作"不修改"—— 误清一个 Key 比少改一个危险得多。
 */
export function changedEnvValues(
  fields: DevEnvField[],
  draft: Record<string, string>,
  cleared: Record<string, boolean> = {},
): Record<string, string> {
  const updates: Record<string, string> = {};
  for (const field of fields) {
    if (cleared[field.key] === true) {
      // 后端把空值定义为"清空该项，回到降级模式"（见 dev_config._validate_value）
      updates[field.key] = "";
      continue;
    }
    const next = draft[field.key];
    if (next === undefined) continue;
    if (field.kind === "secret" && next === "") continue; // 打码值没被改过
    if (next !== field.value) updates[field.key] = next;
  }
  return updates;
}

function isFrontendEnvKey(value: unknown): value is DevFrontendEnvKey {
  if (typeof value !== "object" || value === null) return false;
  const item = value as Record<string, unknown>;
  return (
    typeof item.key === "string" &&
    typeof item.note === "string" &&
    typeof item.is_set === "boolean" &&
    typeof item.source === "string"
  );
}

/** 从生效快照里取出「前端专用配置」那一段。
 *
 * 为什么需要它：面板只写仓库根的 `.env`（后端进程读它），前端变量在这里原本
 * **完全不可见** —— 于是「高德 Web 服务 Key 未配置」很容易被读成「整条地图能力没配」，
 * 而结果页那张地图用的是另一个 Key。这份状态就是为了把这两件事分开。
 *
 * 形状不符时返回 `null`：界面应据此**整块不渲染**，而不是渲染一张空表
 * （"没有前端配置"与"读不到前端配置状态"是两件事）。
 */
export function frontendEnvStatus(effective: Record<string, unknown>): DevFrontendEnv | null {
  const raw = effective.frontend_env;
  if (typeof raw !== "object" || raw === null) return null;
  const candidate = raw as Partial<DevFrontendEnv>;
  if (!Array.isArray(candidate.keys)) return null;
  // ★ 逐字段重建，**不做对象展开**：后端要是多回了一个 `value`
  // （或将来往条目里加了别的字段），也不会被顺手带进界面 ——
  // 「面板拿不到明文 Key」这条纪律不该依赖后端的自觉。
  const keys: DevFrontendEnvKey[] = [];
  for (const item of candidate.keys) {
    if (!isFrontendEnvKey(item)) continue;
    keys.push({ key: item.key, note: item.note, is_set: item.is_set, source: item.source });
  }
  if (keys.length === 0) return null;
  return {
    keys,
    files: Array.isArray(candidate.files)
      ? candidate.files.filter((name): name is string => typeof name === "string")
      : [],
    editable_here: candidate.editable_here === true,
    explanation: typeof candidate.explanation === "string" ? candidate.explanation : "",
  };
}

function authHeaders(token: string): HeadersInit {
  return token === "" ? {} : { "X-Admin-Token": token };
}

export function fetchDevConfig(token: string): Promise<DevConfig> {
  return request<DevConfig>("/api/v1/dev/config", {
    headers: authHeaders(token),
  }).then((result) => result.data);
}

export function updateDevEnv(
  token: string,
  updates: Record<string, string>,
): Promise<string[]> {
  return request<{ changed: string[] }>("/api/v1/dev/env", {
    method: "PUT",
    headers: authHeaders(token),
    body: JSON.stringify({ updates }),
  }).then((result) => result.data.changed);
}

export function updateDevConfigFile(
  token: string,
  name: string,
  content: string,
): Promise<{ name: string; bytes: number }> {
  return request<{ name: string; bytes: number }>(
    `/api/v1/dev/config/${encodeURIComponent(name)}`,
    {
      method: "PUT",
      headers: authHeaders(token),
      body: JSON.stringify({ content }),
    },
  ).then((result) => result.data);
}
