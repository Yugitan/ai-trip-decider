import { createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const projectRoot = path.dirname(fileURLToPath(import.meta.url));

/**
 * ESLint 9 flat config（eslint-config-next）。
 *
 * 环境约束：本机用 pnpm 的严格 node_modules 布局，`@eslint/eslintrc` 和
 * eslint-config-next 依赖的那些插件（@typescript-eslint、eslint-plugin-react…）
 * 都不在 frontend/node_modules 顶层，无法直接 import。
 * 这里改成从各自的依赖树里解析 —— 不新增任何依赖，也不改 package.json。
 */
const eslintRequire = createRequire(require.resolve("eslint/package.json"));
const { FlatCompat } = eslintRequire("@eslint/eslintrc");
const nextConfigDir = path.dirname(
  require.resolve("eslint-config-next/package.json"),
);

const compat = new FlatCompat({
  baseDirectory: projectRoot,
  resolvePluginsRelativeTo: nextConfigDir,
});

const eslintConfig = [
  {
    ignores: [
      "node_modules/**",
      ".next/**",
      "coverage/**",
      "next-env.d.ts",
      "*.tsbuildinfo",
    ],
  },
  ...compat.extends("next/core-web-vitals", "next/typescript"),
  {
    files: ["**/*.{ts,tsx}"],
    rules: {
      // 前端不留任何 console.*：日志走错误 UI，而不是控制台
      "no-console": "error",
      // lib/api.ts 里 `catch (cause) { throw new NetworkError(cause) }` 是约定写法，
      // 不因为「catch 参数没被直接引用」而报噪声（该文件不在本次改动范围内）。
      "@typescript-eslint/no-unused-vars": [
        "warn",
        { argsIgnorePattern: "^_", caughtErrors: "none" },
      ],
    },
  },
];

export default eslintConfig;
