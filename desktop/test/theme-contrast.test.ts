import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

/**
 * 把「注意对比度」这句设计意图变成可执行断言。
 *
 * 见 docs/superpowers/specs/2026-08-14-desktop-visual-production-grade-redesign.md §6：
 * 文档里写一句「注意对比度」没人会去量，写成测试才会在回退时立刻响。
 */

const THEME = readFileSync(
  fileURLToPath(new URL("../src/renderer/theme.css", import.meta.url)),
  "utf8",
);

/** 读出 :root 里某个 token 的字面值。 */
function token(name: string): string {
  const match = THEME.match(new RegExp(`--lobster-${name}:\\s*([^;]+);`));
  const value = match?.[1];
  if (value === undefined) {
    throw new Error(`token --lobster-${name} not found`);
  }
  return value.trim();
}

function channel(value: number): number {
  const c = value / 255;
  return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
}

/** WCAG 相对亮度。只接受 #rrggbb——token 里的颜色都是这个形式。 */
function luminance(hex: string): number {
  const body = hex.replace("#", "");
  if (!/^[0-9a-f]{6}$/i.test(body)) {
    throw new Error(`expected #rrggbb, got ${hex}`);
  }
  const [r = 0, g = 0, b = 0] = [0, 2, 4].map((i) =>
    channel(parseInt(body.slice(i, i + 2), 16)));
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

export function contrastRatio(foreground: string, background: string): number {
  const a = luminance(foreground);
  const b = luminance(background);
  return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
}

describe("theme contrast", () => {
  // 步数、时间、token 数全部用 text-muted 渲染。它一旦低于 AA，界面上所有
  // 「次要但仍需被读到」的数字就都读不清——这正是 Owner 报的「颜色不突出」。
  it.each(["text-primary", "text-secondary", "text-muted"])(
    "%s reaches WCAG AA on the app background",
    (name) => {
      const ratio = contrastRatio(token(name), token("background"));

      expect(ratio).toBeGreaterThanOrEqual(4.5);
    },
  );

  it("keeps the three text levels visually distinct", () => {
    // 只要求达标是不够的：三档全调成近黑也能过 AA，但层级就没了。
    const background = token("background");
    const primary = contrastRatio(token("text-primary"), background);
    const secondary = contrastRatio(token("text-secondary"), background);
    const muted = contrastRatio(token("text-muted"), background);

    expect(primary).toBeGreaterThan(secondary);
    expect(secondary).toBeGreaterThan(muted);
  });
});

describe("theme typography", () => {
  it("keeps a wide gap between body and bold weight", () => {
    // 445 → 600 只差 155，是 Owner 报的「加粗不突出」的直接原因。
    // 400 → 700 差 300，且 700 是中文字体真实存在的字重，不依赖合成。
    const normal = Number(token("ui-font-weight-normal"));
    const bold = Number(token("ui-font-weight-bold"));

    expect(bold - normal).toBeGreaterThanOrEqual(300);
  });
});
