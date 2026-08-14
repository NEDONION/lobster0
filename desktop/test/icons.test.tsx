import { readdirSync, readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { ChevronRightIcon, CloseIcon } from "../src/renderer/icons";

describe("icons", () => {
  it("render as vectors that inherit the surrounding text colour", () => {
    // currentColor 是「图标跟随文字」的实现点：按钮 hover 变色时图标一起变，
    // 不需要为每个状态再写一条 .icon 规则。
    for (const markup of [
      renderToStaticMarkup(<ChevronRightIcon />),
      renderToStaticMarkup(<CloseIcon />),
    ]) {
      expect(markup).toContain("<svg");
      expect(markup).toContain('stroke="currentColor"');
      // 图标是装饰性的，读屏应当跳过——可访问名字由外层按钮的 aria-label 提供。
      expect(markup).toContain('aria-hidden="true"');
    }
  });

  it("defaults the chevron to a size that is actually visible", () => {
    // 原先是 11px 的文字三角，Owner 的原话是「这个箭头太小了」。
    expect(renderToStaticMarkup(<ChevronRightIcon />)).toContain('width="16"');
  });
});

describe("renderer source", () => {
  it("uses no text glyphs as icons", () => {
    // 文字字形当图标是「不像生产级应用」最直接的来源：字面大小与垂直位置随字体
    // 变化，还会被当作文本参与选中与朗读。两个参考仓库（LobsterAI / ClawX）
    // 没有一处这么做。这条测试防止后续「顺手打一个 ×」。
    const directory = fileURLToPath(new URL("../src/renderer", import.meta.url));
    const glyphs = /[▸▾▴▼▲◂►◄✓✕✖⌄⌃]|(?<![\p{L}\p{N}])×(?![\p{L}\p{N}])/u;

    const offenders = readdirSync(directory)
      .filter((name) => name.endsWith(".tsx"))
      .filter((name) => glyphs.test(readFileSync(`${directory}/${name}`, "utf8")));

    expect(offenders).toEqual([]);
  });
});
