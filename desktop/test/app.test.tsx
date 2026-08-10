import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { App } from "../src/renderer/app";

describe("desktop app shell", () => {
  const markup = renderToStaticMarkup(<App />);

  it("shows the task composer on the first paint", () => {
    expect(markup).toContain('aria-label="任务内容"');
  });

  it("offers new task and recent tasks from the shared sidebar", () => {
    expect(markup).toContain("新建任务");
    expect(markup).toContain("最近任务");
  });

  it("drops the old home entry view", () => {
    expect(markup).not.toContain("home-grid");
    expect(markup).not.toContain("把一件事，完整地交给 MiniClaw");
  });
});
