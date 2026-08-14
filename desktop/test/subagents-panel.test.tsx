import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import type { SubagentSummary } from "../src/common/api";
import { SubagentsPanel } from "../src/renderer/subagents-panel";

const ROSTER: SubagentSummary[] = [
  {
    id: "researcher",
    description: "只读检索与汇总，不改文件",
    maxTurns: 4,
    maxToolCalls: 12,
    timeoutSeconds: 300,
  },
];

function render(overrides: Partial<Parameters<typeof SubagentsPanel>[0]> = {}): string {
  return renderToStaticMarkup(
    <SubagentsPanel error={null} subagents={ROSTER} {...overrides} />,
  );
}

describe("SubagentsPanel", () => {
  it("shows each subagent with its budget", () => {
    const html = render();

    expect(html).toContain("researcher");
    expect(html).toContain("只读检索与汇总，不改文件");
    expect(html).toContain("4");
    expect(html).toContain("12");
    expect(html).toContain("300");
  });

  it("never shows the tool set", () => {
    // 与 subagents.list 不下发工具集是同一个决定：工具集是安全边界的一部分，
    // 铺在界面上只会多一处要与配置保持一致的地方。这条测试防止后续「顺手加上」。
    const html = render();

    expect(html).not.toContain("read_file");
    expect(html).not.toContain("工具集");
  });

  it("explains how to enable delegation when nothing is declared", () => {
    // 空白无法区分「没配」与「功能坏了」——这一路已经因此浪费过多次排查。
    const html = render({ subagents: [] });

    expect(html).toContain("尚未声明");
    // 两步都要说：只做一步会得到一个静默不工作的配置。
    expect(html).toContain("[[subagents]]");
    expect(html).toContain("delegate_task");
  });

  it("says delegation is decided by the main agent, not the user", () => {
    // 避免被误解成「这里可以选用哪个 Agent 回答」。
    expect(render()).toContain("主 Agent");
  });

  it("surfaces a load failure without hiding the roster area", () => {
    const html = render({ error: "子 Agent 列表读取失败。" });

    expect(html).toContain("子 Agent 列表读取失败。");
    expect(html).toContain("researcher");
  });
});
