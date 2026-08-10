import { describe, expect, it } from "vitest";

import type { ServerFrame } from "@lobster0/pi-tui/protocol";

import type { SessionHistory } from "../src/common/api";
import {
  appendDesktopUser,
  cancelDesktopTask,
  continueDesktopApproval,
  createDesktopTaskState,
  hydrateSession,
  reduceDesktopFrame,
} from "../src/renderer/task-state";

function frame(type: string, payload: ServerFrame["payload"]): ServerFrame {
  return { v: 1, type, payload };
}

describe("Desktop task state", () => {
  it("projects streaming and approval through the shared reducer", () => {
    let state = createDesktopTaskState("task-1");
    state = appendDesktopUser(state, "整理报告");
    state = reduceDesktopFrame(state, frame("event.turn_started", { turn_id: 7 }));
    state = reduceDesktopFrame(state, frame("event.model_text_delta", {
      turn_id: 7,
      text: "处理中",
    }));
    state = reduceDesktopFrame(state, frame("event.approval_required", {
      turn_id: 7,
      approval_id: 9,
      call_id: "call-9",
      tool_name: "write_file",
      summary: "写入报告",
      arguments: { path: "report.md" },
      grant_modes: ["once"],
    }));

    expect(state.run.timeline.map((item) => item.kind)).toEqual(["user", "assistant"]);
    expect(state.run.busy).toBe(false);
    expect(state.run.pendingApproval?.approvalId).toBe(9);
    expect(state.status).toBe("waiting_approval");
  });

  it("clears the approval locally only after continuation is accepted", () => {
    let state = createDesktopTaskState("task-1");
    state = reduceDesktopFrame(state, frame("event.approval_required", {
      turn_id: 7,
      approval_id: 9,
      call_id: "call-9",
      tool_name: "write_file",
      summary: "写入报告",
      arguments: {},
      grant_modes: ["once"],
    }));

    state = continueDesktopApproval(state);

    expect(state.run.pendingApproval).toBeNull();
    expect(state.run.busy).toBe(true);
    expect(state.status).toBe("running");
  });

  it("settles locally after Core accepts cancellation", () => {
    let state = createDesktopTaskState("task-1");
    state = reduceDesktopFrame(state, frame("event.turn_started", { turn_id: 7 }));

    state = cancelDesktopTask(state);

    expect(state.status).toBe("cancelled");
    expect(state.run.busy).toBe(false);
    expect(state.run.activeTurnId).toBeNull();
  });

  it.each([
    ["event.turn_finished", "completed", null],
    ["event.turn_failed", "failed", "本轮失败：provider_timeout"],
    ["event.turn_cancelled", "cancelled", null],
    ["event.bridge_error", "failed", "Core 操作失败：core_operation_failed"],
  ] as const)("maps %s to a stable Desktop terminal state", (type, status, error) => {
    let state = createDesktopTaskState("task-1");
    state = reduceDesktopFrame(state, frame("event.turn_started", { turn_id: 7 }));
    state = reduceDesktopFrame(state, frame(type, {
      turn_id: 7,
      content: type === "event.turn_finished" ? "完成" : "",
      error_code: "provider_timeout",
      code: "core_operation_failed",
    }));

    expect(state.status).toBe(status);
    expect(state.error).toBe(error);
  });

  it("hydrates persisted messages and marks interrupted runtime state", () => {
    const history: SessionHistory = {
      sessionKey: "task-1",
      updatedAt: "2026-08-09T00:00:00+00:00",
      turns: [{ turnId: 7, status: "failed", errorCode: "runtime_interrupted" }],
      messages: [
        { role: "user", content: "整理报告", turnId: 7 },
        { role: "assistant", content: "未完成的草稿", turnId: 7 },
      ],
    };

    const state = hydrateSession(history);

    expect(state.run.timeline.map((item) => item.kind)).toEqual(["user", "assistant"]);
    expect(state.run.lastAssistantText).toBe("未完成的草稿");
    expect(state.status).toBe("interrupted");
    expect(state.error).toBe("上次运行意外中断");
  });
});

describe("per-turn telemetry snapshots", () => {
  it("keeps each finished turn's telemetry so every reply can show its own metrics", () => {
    let state = createDesktopTaskState("s");
    state = reduceDesktopFrame(state, frame("event.turn_started", { turn_id: 1 }));
    state = reduceDesktopFrame(
      state,
      frame("event.model_usage", {
        turn_id: 1,
        iteration: 2,
        context_tokens: 900,
        input_tokens: 700,
        output_tokens: 120,
        tool_calls: 3,
      }),
    );
    state = reduceDesktopFrame(
      state,
      frame("event.turn_finished", { turn_id: 1, content: "done", duration_ms: 4200 }),
    );

    const first = state.turnTelemetry[1];
    expect(first?.durationMs).toBe(4200);
    expect(first?.toolCalls).toBe(3);
    expect(first?.iterations).toBe(2);
    expect(first?.inputTokens).toBe(700);
  });

  it("does not let a later turn overwrite an earlier turn's numbers", () => {
    let state = createDesktopTaskState("s");
    state = reduceDesktopFrame(state, frame("event.turn_started", { turn_id: 1 }));
    state = reduceDesktopFrame(
      state,
      frame("event.model_usage", { turn_id: 1, iteration: 1, tool_calls: 5 }),
    );
    state = reduceDesktopFrame(
      state,
      frame("event.turn_finished", { turn_id: 1, content: "a", duration_ms: 1000 }),
    );
    state = reduceDesktopFrame(state, frame("event.turn_started", { turn_id: 2 }));
    state = reduceDesktopFrame(
      state,
      frame("event.model_usage", { turn_id: 2, iteration: 1, tool_calls: 1 }),
    );
    state = reduceDesktopFrame(
      state,
      frame("event.turn_finished", { turn_id: 2, content: "b", duration_ms: 2000 }),
    );

    expect(state.turnTelemetry[1]?.durationMs).toBe(1000);
    expect(state.turnTelemetry[1]?.toolCalls).toBe(5);
    expect(state.turnTelemetry[2]?.durationMs).toBe(2000);
    expect(state.turnTelemetry[2]?.toolCalls).toBe(1);
  });

  it("records nothing for turns that failed or were cancelled", () => {
    let state = createDesktopTaskState("s");
    state = reduceDesktopFrame(state, frame("event.turn_started", { turn_id: 1 }));
    state = reduceDesktopFrame(
      state,
      frame("event.turn_failed", { turn_id: 1, error_code: "provider_error" }),
    );

    expect(state.turnTelemetry[1]).toBeUndefined();
  });

  it("starts empty so a fresh conversation shows no stale metrics", () => {
    expect(createDesktopTaskState("s").turnTelemetry).toEqual({});
  });
});
