"use client";

export interface SegmentedOption<T extends string | number> {
  value: T;
  label: string;
}

/**
 * 分段控件：用原生 radio 实现，保证键盘方向键 / 屏幕阅读器行为正确。
 * 视觉上是一枚「沙色底 + 白色滑块」的胶囊，层次靠颜色而非阴影堆叠。
 */
export function Segmented<T extends string | number>({
  name,
  legend,
  value,
  options,
  onChange,
  hideLegend = false,
  className = "",
}: {
  name: string;
  legend: string;
  value: T;
  options: readonly SegmentedOption<T>[];
  onChange: (next: T) => void;
  /** 图例靠 aria-label 提供时（例如与输入框并排），视觉上隐藏但保留语义 */
  hideLegend?: boolean;
  className?: string;
}) {
  return (
    <fieldset className={`min-w-0 ${className}`}>
      <legend
        className={
          hideLegend
            ? "sr-only"
            : "mb-2 block text-sm font-medium text-ink-soft"
        }
      >
        {legend}
      </legend>
      {/* 控件的圆角语言：胶囊（rounded-full）只给"动作"（按钮 / 导航 / 标签），
          表单控件统一走更克制的 --radius-btn，两者混在同一格里才不会显得花。 */}
      <div className="flex gap-1 rounded-btn border border-line bg-sand p-1">
        {options.map((option) => {
          const id = `${name}-${String(option.value)}`;
          return (
            <label key={id} htmlFor={id} className="min-w-0 flex-1">
              <input
                id={id}
                type="radio"
                name={name}
                value={String(option.value)}
                checked={option.value === value}
                onChange={() => onChange(option.value)}
                className="peer sr-only"
              />
              <span className="flex min-h-11 cursor-pointer items-center justify-center rounded-[8px] px-2 text-sm text-ink-soft transition-colors duration-300 peer-checked:bg-shell peer-checked:font-medium peer-checked:text-teal-dark peer-focus-visible:outline-2 peer-focus-visible:outline-offset-2 peer-focus-visible:outline-teal">
                {option.label}
              </span>
            </label>
          );
        })}
      </div>
    </fieldset>
  );
}
