import type { TimelineItem } from "@lobster0/pi-tui/state";
import { describe, expect, it } from "vitest";

import {
  groupTimeline,
  processSummary,
  toolDetail,
  type ProcessItem,
} from "../src/renderer/timeline-blocks";

function user(id: number): TimelineItem {
  return { kind: "user", id, content: `u${id}` };
}

function assistant(id: number): TimelineItem {
  return { kind: "assistant", id, turnId: 1, content: `a${id}`, streaming: false };
}

function reasoning(id: number): TimelineItem {
  return { kind: "reasoning", id, turnId: 1, content: `r${id}`, expanded: false };
}

function tool(id: number, name = "run_command", summary = "run_command"): TimelineItem {
  return {
    kind: "tool",
    id,
    turnId: 1,
    callId: `c${id}`,
    name,
    summary,
    arguments: {},
    status: "succeeded",
    lifecycle: ["requested"],
    preview: "",
    durationMs: null,
    expanded: false,
  };
}

describe("groupTimeline", () => {
  it("collapses consecutive reasoning and tool items into one process block", () => {
    const blocks = groupTimeline([
      user(1),
      reasoning(2),
      tool(3),
      reasoning(4),
      tool(5),
      assistant(6),
    ]);
    expect(blocks.map((block) => block.kind)).toEqual(["message", "process", "message"]);
    const [, process] = blocks;
    expect(process?.kind === "process" && process.items.map((item) => item.id)).toEqual([
      2, 3, 4, 5,
    ]);
  });

  it("keeps user and assistant messages as their own blocks", () => {
    const blocks = groupTimeline([user(1), assistant(2)]);
    expect(blocks).toHaveLength(2);
    expect(blocks.every((block) => block.kind === "message")).toBe(true);
  });

  it("starts a new process block after each message", () => {
    const blocks = groupTimeline([reasoning(1), assistant(2), reasoning(3)]);
    expect(blocks.map((block) => block.kind)).toEqual(["process", "message", "process"]);
  });

  it("identifies a process block by its first item so the key is stable", () => {
    const blocks = groupTimeline([reasoning(7), tool(8)]);
    expect(blocks[0]?.kind === "process" && blocks[0].id).toBe(7);
  });

  it("counts steps for the collapsed summary", () => {
    const blocks = groupTimeline([reasoning(1), tool(2), tool(3)]);
    expect(blocks[0]?.kind === "process" && blocks[0].items.length).toBe(3);
  });

  it("returns nothing for an empty timeline", () => {
    expect(groupTimeline([])).toEqual([]);
  });
});

describe("toolDetail", () => {
  it("drops the summary when Core sends it identical to the tool name", () => {
    // agent/runner.py 目前发的是 summary === tool_name，直接双渲染会重复两行。
    expect(toolDetail({ name: "run_command", summary: "run_command" })).toBeNull();
  });

  it("keeps a summary that actually adds information", () => {
    expect(toolDetail({ name: "run_command", summary: "run_command git · 2 args" })).toBe(
      "run_command git · 2 args",
    );
  });

  it("ignores whitespace-only and blank summaries", () => {
    expect(toolDetail({ name: "read_file", summary: "" })).toBeNull();
    expect(toolDetail({ name: "read_file", summary: "   " })).toBeNull();
    expect(toolDetail({ name: "read_file", summary: " read_file " })).toBeNull();
  });
});

describe("processSummary", () => {
  const reasoning = { kind: "reasoning", id: 1, turnId: 1, content: "想了很多", expanded: true } as const;
  const tool = (id: number): ProcessItem => ({
    kind: "tool",
    id,
    turnId: 1,
    callId: `c${id}`,
    name: "read_file",
    summary: "",
    arguments: {},
    status: "succeeded",
    lifecycle: ["requested", "started", "finished"],
    preview: "",
    durationMs: null,
    expanded: false,
  });
  const local = { kind: "local", id: 9, content: "已中断", tone: "info" } as const;

  it("never puts a number on thinking alone", () => {
    // Owner 展开「过程 1 步」看到的是整整一屏思考。一整轮的思考只产生一个
    // reasoning 条目，正文再长也是一条——把它数成「1 步」既不准确，也无法
    // 回答折叠块唯一要回答的问题：值不值得展开。
    const summary = processSummary([reasoning]);

    expect(summary).toContain("思考");
    expect(summary).not.toMatch(/\d/u);
  });

  it("counts tool calls, because those really are discrete actions", () => {
    expect(processSummary([tool(1), tool(2), tool(3)])).toBe("3 次工具调用");
  });

  it("names both when the block has thinking and tools", () => {
    const summary = processSummary([reasoning, tool(2), tool(3)]);

    expect(summary).toContain("思考");
    expect(summary).toContain("2 次工具调用");
  });

  it("ignores local notices — they are not things the agent did", () => {
    // 本地提示是界面对用户说的话，不是 Agent 的动作。展开后仍然显示。
    expect(processSummary([tool(1), local])).toBe("1 次工具调用");
  });

  it("still says something when the block holds only a local notice", () => {
    // 空标题无法区分「没有内容」与「坏了」。
    expect(processSummary([local])).not.toBe("");
  });
});
