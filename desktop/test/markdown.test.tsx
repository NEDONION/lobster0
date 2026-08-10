import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { Markdown } from "../src/renderer/markdown";

describe("desktop markdown rendering", () => {
  it("renders GFM tables, bold text, and links as real HTML instead of raw syntax", () => {
    const markup = renderToStaticMarkup(
      <Markdown content={"**标题**\n\n| 项目 | 详情 |\n| --- | --- |\n| a | b |\n\n[打开](https://example.com)"} />,
    );
    expect(markup).not.toContain("**标题**");
    expect(markup).not.toContain("| 项目 | 详情 |");
    expect(markup).toContain("<strong>标题</strong>");
    expect(markup).toContain("<table>");
    expect(markup).toContain("<td>a</td>");
    expect(markup).toContain('href="https://example.com"');
    expect(markup).toContain('target="_blank"');
    expect(markup).toContain('rel="noopener noreferrer"');
  });
});
