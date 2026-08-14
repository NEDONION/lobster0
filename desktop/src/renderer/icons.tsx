/**
 * 最小内联图标集。
 *
 * 为什么不引图标库：见
 * docs/superpowers/specs/2026-08-14-desktop-visual-production-grade-redesign.md §3.4。
 * 需要图标的位置目前屈指可数，内联比新增一个运行时依赖划算；超过约 15 个再考虑。
 *
 * 为什么不用文字字形（三角、叉号那种）：字面大小与垂直位置随字体变化，且会被
 * 当作文本参与选中与朗读。矢量图标尺寸可控、颜色跟随 `currentColor`。
 *
 * test/icons.test.tsx 会扫描本目录禁止字形图标回潮，且**不设例外清单**——
 * 所以这段注释也不能出现那些字符。
 */

interface IconProps {
  /** 边长（px）。默认 16——文字字形时代那个 11px 是 Owner 报「箭头太小」的直接原因。 */
  readonly size?: number;
  readonly className?: string;
}

/**
 * 右向尖角。展开态由 CSS 旋转 90°，而不是换成第二个图标——
 * 换图标没法做过渡，旋转可以，且只需维护一份路径。
 */
export function ChevronRightIcon({ size = 16, className }: IconProps): JSX.Element {
  return (
    <svg
      aria-hidden="true"
      className={className}
      fill="none"
      focusable="false"
      height={size}
      stroke="currentColor"
      strokeLinecap="round"
      strokeLinejoin="round"
      strokeWidth="2"
      viewBox="0 0 24 24"
      width={size}
    >
      <path d="m9 18 6-6-6-6" />
    </svg>
  );
}

/** 关闭 / 移除。 */
export function CloseIcon({ size = 14, className }: IconProps): JSX.Element {
  return (
    <svg
      aria-hidden="true"
      className={className}
      fill="none"
      focusable="false"
      height={size}
      stroke="currentColor"
      strokeLinecap="round"
      strokeLinejoin="round"
      strokeWidth="2"
      viewBox="0 0 24 24"
      width={size}
    >
      <path d="M18 6 6 18M6 6l12 12" />
    </svg>
  );
}
