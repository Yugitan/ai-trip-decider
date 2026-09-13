import type { ButtonHTMLAttributes, ReactNode } from "react";

export type ButtonVariant = "primary" | "secondary" | "quiet";
export type ButtonSize = "md" | "lg";

/**
 * 所有按钮的共同底线：
 * - min-h-11（44px）满足移动端可点区域要求
 * - loading 时不仅禁用，还必须给出可见反馈（旋转指示 + 文案由调用方切换）
 *
 * 视觉：胶囊形 + 主色走「墨色实底」。品牌青绿留给链接、选中态与图表，
 * 而不是每个按钮 —— 一页里只该有一个最重的动作。
 * hover 只做 1.02 的缩放（`enabled:` 保证禁用态不动）与颜色过渡。
 */
const BASE_CLASS =
  "inline-flex min-h-11 items-center justify-center gap-2 rounded-full font-medium transition-[transform,background-color,border-color,color] duration-300 ease-out enabled:hover:scale-[1.02] motion-reduce:enabled:hover:scale-100 disabled:cursor-not-allowed disabled:opacity-60";

const VARIANT_CLASS: Record<ButtonVariant, string> = {
  primary: "bg-ink text-sand hover:bg-ink/90",
  secondary: "border border-line bg-shell/70 text-ink backdrop-blur hover:border-ink/25",
  quiet: "text-teal-dark hover:bg-teal-tint",
};

const SIZE_CLASS: Record<ButtonSize, string> = {
  md: "px-5 text-sm",
  lg: "px-7 text-base",
};

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  /** 提交中：按钮禁用 + aria-busy + 显示旋转指示 */
  loading?: boolean;
  /** 占满父容器宽度（移动端主 CTA） */
  block?: boolean;
  children: ReactNode;
}

export function Button({
  variant = "primary",
  size = "lg",
  loading = false,
  block = false,
  className = "",
  children,
  disabled,
  type = "button",
  ...rest
}: ButtonProps) {
  const classes = [
    BASE_CLASS,
    VARIANT_CLASS[variant],
    SIZE_CLASS[size],
    block ? "w-full" : "",
    className,
  ]
    .filter((part) => part.length > 0)
    .join(" ");

  return (
    <button
      {...rest}
      type={type}
      disabled={disabled === true || loading}
      aria-busy={loading ? true : undefined}
      className={classes}
    >
      {loading ? (
        <span
          aria-hidden="true"
          className="size-4 shrink-0 animate-spin rounded-full border-2 border-current border-t-transparent"
        />
      ) : null}
      {children}
    </button>
  );
}
