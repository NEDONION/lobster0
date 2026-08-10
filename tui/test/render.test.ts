import assert from "node:assert/strict";
import test from "node:test";

import { visibleWidth } from "@earendil-works/pi-tui";

import { HeaderLine, TelemetryLine, TimelineView } from "../dist/components/conversation.js";
import { appendUser, createInitialState, reduceFrame } from "../dist/state.js";
import type { ServerFrame } from "../dist/protocol.js";

function frame(type: string, payload: Record<string, unknown>): ServerFrame {
  return { v: 1, type, payload };
}

test("dense Chinese transcript renders roles and activity without oversized cards", () => {
  let state = appendUser(createInitialState(), "帮我统计本周创建了多少飞书文档");
  state = reduceFrame(state, frame("event.turn_started", { turn_id: 4, session_id: 1 }));
  state = reduceFrame(
    state,
    frame("event.model_reasoning", { turn_id: 4, text: "先确认 lark-cli 是否可用。" }),
  );
  state = reduceFrame(
    state,
    frame("event.tool_requested", {
      turn_id: 4,
      call_id: "tool-4",
      tool_name: "run_command",
      summary: "lark-cli doc list",
      arguments: { program: "/usr/local/bin/lark-cli", args: ["doc", "list"] },
    }),
  );
  state = reduceFrame(
    state,
    frame("event.tool_finished", {
      turn_id: 4,
      call_id: "tool-4",
      tool_name: "run_command",
      status: "succeeded",
      preview: "17 documents",
      duration_ms: 42,
    }),
  );
  state = reduceFrame(
    state,
    frame("event.turn_finished", {
      turn_id: 4,
      status: "completed",
      content: "这周一共创建了 **17 篇**文档。",
      duration_ms: 842,
    }),
  );
  const view = new TimelineView(state, "zh-CN");

  for (const width of [64, 80, 120]) {
    const lines = view.render(width);
    assert.equal(lines.every((line) => visibleWidth(line) <= width), true);
    const output = lines.join("\n");
    assert.match(output, /你/);
    assert.match(output, /Lobster0/);
    assert.match(output, /思考/);
    assert.match(output, /run_command/);
    assert.match(output, /42 ms/);
    assert.doesNotMatch(output, /╭|╰/);
  }
});

test("header and telemetry remain single-line and truncate at narrow widths", () => {
  const header = new HeaderLine(
    "0.1.0",
    "deepseek-v4-pro",
    "default",
    "workspace",
    "zh-CN",
    "autopilot",
  );
  const telemetry = new TelemetryLine(
    {
      contextTokens: 12_400,
      inputTokens: 10_800,
      outputTokens: 1_600,
      toolCalls: 2,
      iterations: 3,
      durationMs: 842,
      providerRequestId: "req-13",
    },
    128_000,
    "zh-CN",
  );

  for (const width of [48, 80, 120]) {
    const headerLines = header.render(width);
    const telemetryLines = telemetry.render(width);
    assert.equal(headerLines.length, 1);
    assert.equal(telemetryLines.length, 1);
    assert.equal(visibleWidth(headerLines[0] ?? "") <= width, true);
    assert.equal(visibleWidth(telemetryLines[0] ?? "") <= width, true);
  }
  assert.match(header.render(120).join("\n"), /AUTOPILOT/);
});
