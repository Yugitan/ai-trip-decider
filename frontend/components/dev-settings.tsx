"use client";

import { useCallback, useEffect, useState } from "react";

import {
  changedEnvValues,
  fetchDevConfig,
  groupEnvFields,
  readAdminToken,
  saveAdminToken,
  updateDevConfigFile,
  updateDevEnv,
  type DevConfig,
  type DevEnvField,
} from "@/lib/dev-api";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";

/**
 * 开发设置面板（**不是用户功能**）。
 *
 * 它只在开发环境存在：后端的 `/api/v1/dev/*` 在 `ENV!=development` 时根本不注册，
 * 因此生产环境打开这个页面只会看到"接口不存在"。首页/导航里**没有**任何入口指向它。
 *
 * 界面上的三条诚实性要求：
 * 1. 顶部必须写明"这是开发工具、生产不可用"，避免有人以为用户也能看到；
 * 2. Secret 只显示打码值：**留空 = 不修改**（默认语义），想清空必须点"清空该项"
 *    —— 空字符串在这两者之间没有第三种解释空间，所以清空得是一个显式动作；
 * 3. 保存结果如实回报（改了哪些键 / 校验失败的原因），不做"看起来保存成功"。
 *    注意保存成功后要**重新拉取**生效状态，但重新拉取不能顺手把提示清掉 ——
 *    "保存成功但看不到反馈"和失败一样难受。
 */

const SECRET_PLACEHOLDER = "（已配置，留空表示不修改）";

export function DevSettings() {
  const [token, setToken] = useState("");
  const [config, setConfig] = useState<DevConfig | null>(null);
  const [draft, setDraft] = useState<Record<string, string>>({});
  /** 被显式要求清空的 Secret（与"没碰过的空输入框"区分开）。 */
  const [cleared, setCleared] = useState<Record<string, boolean>>({});
  const [selectedFile, setSelectedFile] = useState<string>("");
  const [fileDraft, setFileDraft] = useState<string>("");
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<{ kind: "ok" | "error"; text: string } | null>(null);

  /** 重新拉取配置；返回是否成功（调用方靠它决定要不要覆盖提示）。
   *
   * ``keepNote`` 用于"保存后的刷新"：此时不该把刚刚成功的提示抹掉。
   */
  const load = useCallback(
    async (adminToken: string, options?: { keepNote?: boolean }): Promise<boolean> => {
      if (options?.keepNote !== true) setNote(null);
      setBusy(true);
      try {
        const loaded = await fetchDevConfig(adminToken);
        setConfig(loaded);
        setDraft({});
        setCleared({});
        const first = loaded.config_files[0];
        setSelectedFile(first?.name ?? "");
        setFileDraft(first?.content ?? "");
        return true;
      } catch (error: unknown) {
        setConfig(null);
        setNote({ kind: "error", text: describeError(error) });
        return false;
      } finally {
        setBusy(false);
      }
    },
    [],
  );

  useEffect(() => {
    const stored = readAdminToken();
    setToken(stored);
    // Token 存在（或后端允许本机免 Token）时才尝试加载；失败会显示原因而不是空白页
    void load(stored);
  }, [load]);

  function handleTokenSubmit() {
    saveAdminToken(token);
    void load(token);
  }

  async function handleSaveEnv() {
    if (config === null) return;
    const updates = changedEnvValues(config.env, draft, cleared);
    if (Object.keys(updates).length === 0) {
      setNote({ kind: "ok", text: "没有改动需要保存。" });
      return;
    }
    setBusy(true);
    try {
      const changed = await updateDevEnv(token, updates);
      // 先刷新再报结果：加载失败时 load 留下的错误提示不能被"保存成功"盖掉
      if (await load(token, { keepNote: true })) {
        setNote({
          kind: "ok",
          text:
            changed.length === 0
              ? "值没有变化，未写入。"
              : `已更新并立即生效：${changed.join("、")}。Secret 类改动重启后端后也会保留。`,
        });
      }
    } catch (error: unknown) {
      setNote({ kind: "error", text: describeError(error) });
    } finally {
      setBusy(false);
    }
  }

  async function handleSaveFile() {
    if (selectedFile === "") return;
    setBusy(true);
    try {
      const result = await updateDevConfigFile(token, selectedFile, fileDraft);
      if (await load(token, { keepNote: true })) {
        setNote({
          kind: "ok",
          text: `${result.name} 已保存（${result.bytes} 字节）并通过配置校验。`,
        });
      }
    } catch (error: unknown) {
      setNote({
        kind: "error",
        text: `${describeError(error)} —— 文件未被修改，可以改完再试。`,
      });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-5">
      <Card className="px-5 py-5 sm:px-7">
        <h1 className="text-lg font-semibold tracking-tight text-ink">开发设置</h1>
        <p className="mt-2 text-sm leading-relaxed text-ink-soft">
          {config?.notice ??
            "这是开发期的配置面板，不是用户功能：生产环境不会注册这些接口（访问即 404）。"}
        </p>
        <p className="mt-1.5 text-xs leading-relaxed text-ink-faint">
          修改会写入项目根目录的 <code>.env</code> 或 <code>config/*.yaml</code>，
          YAML 一律**先校验后落盘**，不通过不会改动文件。密钥只写不读，页面拿不到明文。
        </p>

        <div className="mt-4 flex flex-col gap-2 sm:flex-row sm:items-end">
          <label className="flex-1 text-xs font-medium text-ink-soft">
            ADMIN_TOKEN（未设置时仅本机可访问）
            <input
              type="password"
              value={token}
              onChange={(event) => setToken(event.currentTarget.value)}
              placeholder=".env 里的 ADMIN_TOKEN"
              className="mt-1.5 w-full rounded-btn border border-line bg-shell px-3 py-2 text-sm text-ink outline-none focus:border-teal"
            />
          </label>
          <Button type="button" onClick={handleTokenSubmit} loading={busy}>
            加载配置
          </Button>
        </div>

        {note === null ? null : (
          <p
            role="status"
            aria-live="polite"
            className={`mt-3 rounded-[10px] border px-3 py-2 text-xs leading-relaxed ${
              note.kind === "ok"
                ? "border-teal/30 bg-teal-tint text-teal-dark"
                : "border-coral/40 bg-coral-tint text-coral"
            }`}
          >
            {note.text}
          </p>
        )}
      </Card>

      {config === null ? null : (
        <>
          <Card className="px-5 py-5 sm:px-7">
            <h2 className="text-sm font-semibold text-ink">环境变量</h2>
            <p className="mt-1 text-xs text-ink-faint">
              只列出白名单内的键；<code>DATABASE_URL</code> / <code>SESSION_SECRET</code>
              {" / "}
              <code>ENV</code> 刻意不在其中（改它们要么必须重启，要么等于关掉本页面）。
            </p>

            <div className="mt-4 space-y-5">
              {groupEnvFields(config.env).map((group) => (
                <div key={group.group}>
                  <p className="text-xs font-semibold text-ink-soft">{group.group}</p>
                  <div className="mt-2 grid gap-3 sm:grid-cols-2">
                    {group.fields.map((field) => (
                      <EnvInput
                        key={field.key}
                        field={field}
                        draft={draft[field.key]}
                        cleared={cleared[field.key] === true}
                        onChange={(value) =>
                          setDraft((current) => ({ ...current, [field.key]: value }))
                        }
                        onToggleClear={(next) =>
                          setCleared((current) => ({ ...current, [field.key]: next }))
                        }
                      />
                    ))}
                  </div>
                </div>
              ))}
            </div>

            <div className="mt-4 flex items-center gap-3">
              <Button type="button" onClick={handleSaveEnv} loading={busy}>
                保存环境变量
              </Button>
              <span className="text-xs text-ink-faint">
                只提交改动过的项；Secret 留空表示不修改，要清空请点该行的「清空该项」。
              </span>
            </div>
          </Card>

          <Card className="px-5 py-5 sm:px-7">
            <h2 className="text-sm font-semibold text-ink">配置文件（config/*.yaml）</h2>
            <div className="mt-3 flex flex-wrap items-center gap-2">
              {config.config_files.map((file) => (
                <button
                  key={file.name}
                  type="button"
                  onClick={() => {
                    setSelectedFile(file.name);
                    setFileDraft(file.content);
                  }}
                  className={`rounded-full border px-3 py-1.5 text-xs transition-colors ${
                    file.name === selectedFile
                      ? "border-teal bg-teal-tint text-teal-dark"
                      : "border-line bg-shell text-ink-soft hover:border-ink/25"
                  }`}
                >
                  {file.name}
                </button>
              ))}
            </div>

            <textarea
              aria-label="配置文件内容"
              value={fileDraft}
              onChange={(event) => setFileDraft(event.currentTarget.value)}
              spellCheck={false}
              rows={18}
              className="tnum mt-3 w-full rounded-btn border border-line bg-sand p-3 font-mono text-xs leading-relaxed text-ink outline-none focus:border-teal"
            />

            <div className="mt-3 flex items-center gap-3">
              <Button type="button" onClick={handleSaveFile} loading={busy}>
                保存 {selectedFile || "文件"}
              </Button>
              <span className="text-xs text-ink-faint">
                保存前会用该文件的强校验模型检查内容，不通过则一个字节都不写。
              </span>
            </div>
          </Card>

          <Card className="px-5 py-5 sm:px-7">
            <h2 className="text-sm font-semibold text-ink">当前生效的配置</h2>
            <p className="mt-1 text-xs text-ink-faint">
              这份快照来自运行中的进程：改完这里如果状态没变，说明改动没有生效。
            </p>
            <pre className="tnum mt-3 max-w-full overflow-x-auto rounded-btn border border-line bg-sand p-3 text-xs leading-relaxed text-ink">
              <code data-testid="effective-config">
                {JSON.stringify(config.effective, null, 2)}
              </code>
            </pre>
          </Card>
        </>
      )}
    </div>
  );
}

function EnvInput({
  field,
  draft,
  cleared,
  onChange,
  onToggleClear,
}: {
  field: DevEnvField;
  draft: string | undefined;
  cleared: boolean;
  onChange: (value: string) => void;
  onToggleClear: (next: boolean) => void;
}) {
  const value = draft ?? (field.kind === "secret" ? "" : field.value);
  const canClear = field.kind === "secret" && field.is_set;

  return (
    <div className="text-xs font-medium text-ink-soft">
      <label>
        {field.label}
        <span className="ml-1.5 font-normal text-ink-faint">{field.key}</span>
        {field.choices.length > 0 ? (
          <select
            aria-label={field.key}
            value={value}
            onChange={(event) => onChange(event.currentTarget.value)}
            className="mt-1.5 w-full rounded-btn border border-line bg-shell px-3 py-2 text-sm text-ink outline-none focus:border-teal"
          >
            <option value="">（不修改）</option>
            {field.choices.map((choice) => (
              <option key={choice} value={choice}>
                {choice}
              </option>
            ))}
          </select>
        ) : (
          <input
            aria-label={field.key}
            type={field.kind === "secret" ? "password" : "text"}
            value={cleared ? "" : value}
            disabled={cleared}
            placeholder={
              field.kind === "secret"
                ? field.is_set
                  ? SECRET_PLACEHOLDER
                  : "（未配置）"
                : field.hint
            }
            onChange={(event) => onChange(event.currentTarget.value)}
            className="mt-1.5 w-full rounded-btn border border-line bg-shell px-3 py-2 text-sm text-ink outline-none focus:border-teal disabled:opacity-50"
          />
        )}
      </label>

      {field.hint === "" ? null : (
        <span className="mt-1 block font-normal text-ink-faint">{field.hint}</span>
      )}

      {canClear ? (
        <span className="mt-1 flex flex-wrap items-center gap-2 font-normal text-ink-faint">
          <span className="tnum">当前值：{field.value}（只写不读）</span>
          <button
            type="button"
            aria-pressed={cleared}
            onClick={() => onToggleClear(!cleared)}
            className={`inline-flex min-h-6 items-center rounded-full border px-2 py-0.5 text-[11px] transition-colors ${
              cleared
                ? "border-coral/50 bg-coral-tint text-coral"
                : "border-line text-ink-soft hover:border-coral/50 hover:text-coral"
            }`}
          >
            {cleared ? "取消清空" : "清空该项"}
          </button>
          {cleared ? (
            <span className="text-coral">保存后该项会被清空（回到降级模式）</span>
          ) : null}
        </span>
      ) : null}
    </div>
  );
}

function describeError(error: unknown): string {
  if (typeof error === "object" && error !== null) {
    const candidate = error as { code?: unknown; message?: unknown; hint?: unknown };
    if (typeof candidate.message === "string" && candidate.message !== "") {
      const hint = typeof candidate.hint === "string" && candidate.hint !== "" ? `（${candidate.hint}）` : "";
      return `${candidate.message}${hint}`;
    }
    if (typeof candidate.code === "string") return `请求失败：${candidate.code}`;
  }
  return "请求失败：面板仅在开发环境可用，且需要正确的 ADMIN_TOKEN。";
}
