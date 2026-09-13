"use client";

import type { ReactNode } from "react";

const CHIP_BASE =
  "inline-flex min-h-11 items-center rounded-full px-4 text-sm transition-colors duration-300";

/**
 * 多选标签（偏好）。用原生 checkbox 承载语义，视觉由 peer-checked 驱动：
 * 键盘可用、屏幕阅读器可读、点击区域 ≥ 44px。
 * 选中态用品牌青绿的浅底 + 深字，而不是实心高饱和色块。
 */
export function ChipToggle({
  id,
  name,
  label,
  emoji,
  checked,
  onChange,
}: {
  id: string;
  name: string;
  label: string;
  emoji: string;
  checked: boolean;
  onChange: (next: boolean) => void;
}) {
  return (
    <label htmlFor={id} className="inline-flex">
      <input
        id={id}
        name={name}
        type="checkbox"
        checked={checked}
        onChange={(event) => onChange(event.currentTarget.checked)}
        className="peer sr-only"
      />
      <span
        className={`${CHIP_BASE} gap-2 border border-line bg-shell/60 text-ink-soft hover:border-ink/20 hover:text-ink peer-checked:border-teal/40 peer-checked:bg-teal-tint peer-checked:font-medium peer-checked:text-teal-dark peer-focus-visible:outline-2 peer-focus-visible:outline-offset-2 peer-focus-visible:outline-teal`}
      >
        <span aria-hidden="true" className="text-[15px] leading-none">
          {emoji}
        </span>
        {label}
      </span>
    </label>
  );
}

/**
 * 一键填入的示例标签（按钮语义，不是选择状态）。
 */
export function ChipButton({
  children,
  onClick,
  ariaLabel,
}: {
  children: ReactNode;
  onClick: () => void;
  ariaLabel?: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={ariaLabel}
      className={`${CHIP_BASE} gap-2 border border-dashed border-line/90 bg-transparent text-left text-ink-soft hover:border-teal/50 hover:bg-teal-tint/60 hover:text-teal-dark`}
    >
      {children}
    </button>
  );
}
