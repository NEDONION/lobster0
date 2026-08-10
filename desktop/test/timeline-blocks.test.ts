import type { TimelineItem } from "@lobster0/pi-tui/state";
import { describe, expect, it } from "vitest";

import { groupTimeline, toolDetail } from "../src/renderer/timeline-blocks";

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
