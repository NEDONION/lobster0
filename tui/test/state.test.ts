import assert from "node:assert/strict";
import test from "node:test";

import { createInitialState, reduceFrame } from "../dist/state.js";
import type { ServerFrame } from "../dist/protocol.js";

function frame(type: string, payload: Record<string, unknown>): ServerFrame {
  return { v: 1, type, payload };
}

test("one assistant entry absorbs one hundred deltas and final markdown", () => {
  let state = createInitialState();
  state = reduceFrame(state, frame("event.turn_started", { turn_id: 9, session_id: 3 }));
  for (let index = 0; index < 100; index += 1) {
    state = reduceFrame(
      state,
      frame("event.model_text_delta", { turn_id: 9, text: String(index % 10) }),
    );
  }
  state = reduceFrame(
    state,
    frame("event.turn_finished", {
      turn_id: 9,
      status: "completed",
      content: "最终 **答案**",
      duration_ms: 240,
    }),
  );

  const assistants = state.timeline.filter((item) => item.kind === "assistant");
  assert.equal(assistants.length, 1);
  assert.equal(assistants[0]?.content, "最终 **答案**");
  assert.equal(assistants[0]?.streaming, false);
});

test("reasoning and tool lifecycle form detailed activity items", () => {
  let state = createInitialState();
  state = reduceFrame(
    state,
    frame("event.model_reasoning", { turn_id: 11, text: "先检查 CLI" }),
  );
  state = reduceFrame(
    state,
    frame("event.tool_requested", {
      turn_id: 11,
      call_id: "tool-1",
      tool_name: "run_command",
      summary: "lark-cli doc list",
      arguments: { program: "/usr/local/bin/lark-cli", args: ["doc", "list"] },
    }),
  );
  state = reduceFrame(
    state,
    frame("event.tool_started", {
      turn_id: 11,
      call_id: "tool-1",
      tool_name: "run_command",
    }),
  );
  state = reduceFrame(
    state,
    frame("event.tool_finished", {
      turn_id: 11,
      call_id: "tool-1",
      tool_name: "run_command",
      status: "succeeded",
      preview: "17 documents",
      duration_ms: 42,
    }),
  );

  const reasoning = state.timeline.find((item) => item.kind === "reasoning");
  const tool = state.timeline.find((item) => item.kind === "tool");
  assert.equal(reasoning?.expanded, true);
  assert.equal(reasoning?.content, "先检查 CLI");
  assert.equal(tool?.status, "succeeded");
  assert.equal(tool?.durationMs, 42);
  assert.deepEqual(tool?.lifecycle, ["requested", "started", "finished"]);
  assert.equal(tool?.preview, "17 documents");
});

test("usage and approval state retain only Core-published values", () => {
  let state = createInitialState();
  state = reduceFrame(
    state,
    frame("event.model_usage", {
      turn_id: 13,
      context_tokens: 12_400,
      input_tokens: 10_800,
      output_tokens: 1_600,
      tool_calls: 2,
      iteration: 3,
      provider_request_id: "req-provider-13",
    }),
  );
  state = reduceFrame(
    state,
    frame("event.approval_required", {
      turn_id: 13,
      approval_id: 7,
      call_id: "tool-7",
      tool_name: "run_command",
      summary: "run lark-cli",
      arguments: { program: "/usr/local/bin/lark-cli", args: ["doc"] },
      grant_modes: ["once", "session"],
    }),
  );

  assert.deepEqual(state.telemetry, {
    contextTokens: 12_400,
    inputTokens: 10_800,
    outputTokens: 1_600,
    toolCalls: 2,
    iterations: 3,
    durationMs: null,
    providerRequestId: "req-provider-13",
  });
  assert.deepEqual(state.pendingApproval?.grantModes, ["once", "session"]);
  assert.equal(state.busy, false);
});

test("browser activity never retains typed text or unstable refs", () => {
  const state = reduceFrame(
    createInitialState(),
    frame("event.tool_requested", {
      turn_id: 17,
      call_id: "browser-1",
      tool_name: "browser_type",
      summary: "browser_type",
      arguments: {
        origin: "https://example.com",
        generation: "private-generation",
        ref: "@e7",
        role: "textbox",
        input_kind: "text",
        text: "private typed value",
      },
    }),
  );

  const tool = state.timeline.find((item) => item.kind === "tool");
  assert.deepEqual(tool?.arguments, {
    origin: "https://example.com",
    role: "textbox",
    input_kind: "text",
    text: "<redacted>",
  });
  assert.equal(JSON.stringify(tool).includes("private typed value"), false);
  assert.equal(JSON.stringify(tool).includes("private-generation"), false);
});
