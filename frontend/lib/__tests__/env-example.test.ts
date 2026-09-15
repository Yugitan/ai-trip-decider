import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

/**
 * 前端环境变量模板的漂移检查（**代码里读的 env ↔ `frontend/.env.example`**）。
 *
 * 与后端 `tests/unit/test_env_hygiene.py` 是同一件事的另一半：那边守根模板 ↔ `Settings`，
 * 这边守前端模板 ↔ 真正读 `process.env` 的那几行代码。
 *
 * 为什么需要它：
 *   1. **前端有自己的一套 env**，而且 Next 只读 `frontend/` 下的 `.env*` ——
 *      写在哪儿会生效、写在哪儿是"看着配好了其实没读"，靠记忆一定会错（已经踩过）；
 *   2. 加一个 `NEXT_PUBLIC_*` 等于**把它写进前端产物**，而本项目的铁律是
 *      「任何 Key 只存在于后端」；已知例外只有高德 JS API Key 那一个，
 *      所以"哪些变量允许浏览器看见"必须是钉死的清单，而不是随手的决定。
 */

const frontendDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const templatePath = path.join(frontendDir, ".env.example");

/** 扫描范围：会进入产物的源码目录，加上构建期也会读 env 的 `next.config.ts`。 */
const SOURCE_DIRS = ["app", "components", "lib"];
const EXTRA_FILES = ["next.config.ts"];

/** `process.env.NAME` 与 `process.env["NAME"]` 两种写法都要认。 */
const ENV_REFERENCE =
  /process\.env(?:\.([A-Za-z_][A-Za-z0-9_]*)|\[\s*["'`]([A-Za-z_][A-Za-z0-9_]*)["'`]\s*\])/g;

/** 由 Next / 构建链自己注入，不属于本项目配置的变量。 */
const RUNTIME_PROVIDED = new Set(["NODE_ENV"]);

/**
 * **允许被浏览器看见**的变量清单 —— 故意写死在这里。
 *
 * 加一个 `NEXT_PUBLIC_*` 需要一次有意识的审查（它会被内联进前端产物），
 * 这个清单的作用就是让那次审查无法被顺手忽略：改了它，测试才会绿。
 */
const BROWSER_VISIBLE = [
  "NEXT_PUBLIC_API_BASE_URL",
  "NEXT_PUBLIC_HERO_VIDEO_URL",
  "NEXT_PUBLIC_AMAP_JS_KEY",
];

function sourceFiles(dir: string, out: string[] = []): string[] {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    if (entry.name === "node_modules" || entry.name === ".next" || entry.name === "__tests__") {
      continue;
    }
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      sourceFiles(full, out);
    } else if (/\.tsx?$/.test(entry.name) && !/\.test\./.test(entry.name)) {
      out.push(full);
    }
  }
  return out;
}

function scannedFiles(): string[] {
  return [
    ...SOURCE_DIRS.flatMap((dir) => sourceFiles(path.join(frontendDir, dir))),
    ...EXTRA_FILES.map((file) => path.join(frontendDir, file)),
  ];
}

/** 环境变量名 → 读它的文件（相对 `frontend/`），用来把失败信息说得具体。 */
function envReferences(): Map<string, string[]> {
  const found = new Map<string, string[]>();
  for (const file of scannedFiles()) {
    const relative = path.relative(frontendDir, file);
    for (const match of fs.readFileSync(file, "utf-8").matchAll(ENV_REFERENCE)) {
      const name = match[1] ?? match[2];
      if (!name) continue;
      found.set(name, [...(found.get(name) ?? []), relative]);
    }
  }
  return found;
}

/** 模板里出现的变量名：活键（`KEY=`）与注释示例（`# KEY=`）都算"写了"。 */
function documentedNames(): Set<string> {
  const text = fs.readFileSync(templatePath, "utf-8");
  return new Set(
    [...text.matchAll(/^#?\s*([A-Z][A-Z0-9_]*)=/gm)].map((match) => match[1] as string),
  );
}

const referenced = envReferences();

describe("frontend/.env.example", () => {
  it("代码里读的每个环境变量都在模板里写了", () => {
    const documented = documentedNames();
    const missing = [...referenced.keys()]
      .filter((name) => !RUNTIME_PROVIDED.has(name) && !documented.has(name))
      .sort();

    expect(
      missing,
      `这些变量代码在读，但 frontend/.env.example 里没有：${missing.join(", ")}。` +
        "模板是使用者唯一会看的清单，加变量就要同步写进去。",
    ).toEqual([]);
  });

  it("模板里写的每个变量都真的被代码读到（没有过期的开关）", () => {
    const stale = [...documentedNames()].filter((name) => !referenced.has(name)).sort();

    expect(
      stale,
      `frontend/.env.example 里这些变量没有任何代码在读：${stale.join(", ")}。` +
        "过期的开关比没有更糟 —— 填了它的人会以为生效了。",
    ).toEqual([]);
  });

  it("浏览器可见的变量清单是钉死的（加一个就要改这里）", () => {
    const publicNames = [...referenced.keys()]
      .filter((name) => name.startsWith("NEXT_PUBLIC_"))
      .sort();

    expect(publicNames).toEqual([...BROWSER_VISIBLE].sort());
  });
});

describe("高德安全密钥", () => {
  it("只在服务端代理路由里被读，且从不带 NEXT_PUBLIC_ 前缀", () => {
    expect(referenced.get("AMAP_SECURITY_CODE")).toEqual(["app/amap-proxy/[...path]/route.ts"]);

    // 带 NEXT_PUBLIC_ 前缀等同于点名要 Next 把它内联进前端产物，
    // 那样代理层就白设了 —— 任何文件里出现这个名字都算失败。
    const offenders = scannedFiles()
      .filter((file) => fs.readFileSync(file, "utf-8").includes("NEXT_PUBLIC_AMAP_SECURITY"))
      .map((file) => path.relative(frontendDir, file));
    expect(offenders).toEqual([]);
  });
});
