import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { App, sessionStatusLabel } from "../src/renderer/app";

describe("desktop app shell", () => {
  const markup = renderToStaticMarkup(<App />);

  it("shows the task composer on the first paint", () => {
    expect(markup).toContain('aria-label="消息内容"');
  });

  it("offers new conversation and recent conversations from the shared sidebar", () => {
    expect(markup).toContain("新建对话");
    expect(markup).toContain("最近对话");
  });

  it("drops the old home entry view", () => {
    expect(markup).not.toContain("home-grid");
    expect(markup).not.toContain("把一件事，完整地交给 Lobster0");
  });

  it("keeps the empty conversation to a single centred invite", () => {
    // 空态下不应再出现与居中邀请标题重复的顶栏。
    expect(markup).toContain("今天想完成什么？");
    expect(markup).not.toContain("conversation-header");
    expect(markup).not.toContain("新对话</h1>");
  });
});

describe("sidebar session status labels", () => {
  it("hides the completed status because it is the common case", () => {
    expect(sessionStatusLabel("completed")).toBeNull();
  });

  it("translates statuses that need the user's attention", () => {
    expect(sessionStatusLabel("waiting_approval")).toBe("待审批");
    expect(sessionStatusLabel("failed")).toBe("失败");
    expect(sessionStatusLabel("running")).toBe("运行中");
    expect(sessionStatusLabel("cancelled")).toBe("已取消");
  });

  it("passes unknown statuses through instead of silently dropping them", () => {
    expect(sessionStatusLabel("some_new_core_status")).toBe("some_new_core_status");
  });
});
