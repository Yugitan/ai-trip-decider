import type { ReactNode } from "react";

/**
 * 卡片：默认只留一条发丝线，不投影。
 * 层次靠留白与边框建立，需要「抬起来」的场景（规划卡）由调用方显式加 shadow-lift。
 */
export function Card({
  children,
  className = "",
}: {
  children: ReactNode;
  className?: string;
}) {
  return (
    <div className={`rounded-card border border-line bg-shell ${className}`}>
      {children}
    </div>
  );
}

/**
 * 区块标题：h2 + 可选说明。全站只允许 Hero 出现一个 h1。
 * 标题走展示体（衬线），说明文字保持 UI 体 —— 两种字体交替出现，层级才立得住。
 */
export function SectionHeading({
  eyebrow,
  title,
  description,
  id,
}: {
  eyebrow?: string;
  title: string;
  description?: string;
  id?: string;
}) {
  return (
    <div className="max-w-3xl">
      {eyebrow ? (
        <p className="flex items-center gap-2.5 text-xs tracking-[0.14em] text-ink-soft uppercase">
          <span aria-hidden="true" className="h-px w-6 bg-line" />
          {eyebrow}
        </p>
      ) : null}
      <h2
        id={id}
        className="mt-5 text-3xl leading-[1.15] tracking-[-0.01em] text-ink sm:text-4xl"
      >
        {title}
      </h2>
      {description ? (
        <p className="mt-4 max-w-2xl text-sm leading-relaxed text-ink-soft sm:text-base">
          {description}
        </p>
      ) : null}
    </div>
  );
}
