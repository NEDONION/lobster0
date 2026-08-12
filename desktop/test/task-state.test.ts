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

describe("streaming paragraph breaks", () => {
  it("separates text produced across tool-calling rounds", () => {
    // 每轮迭代的文字都追加到同一个 assistant item，Core 不发换行，
    // 直接拼接会糊成一大段；工具调用之后的新文字应当另起一段。
    let state = createDesktopTaskState("s");
    state = reduceDesktopFrame(state, frame("event.turn_started", { turn_id: 1 }));
    state = reduceDesktopFrame(
      state,
      frame("event.model_text_delta", { turn_id: 1, text: "先查一下命令。" }),
    );
    state = reduceDesktopFrame(
      state,
      frame("event.tool_requested", {
        turn_id: 1,
        call_id: "c1",
        tool_name: "run_command",
        summary: "run_command x · 1 arg",
        arguments: {},
      }),
    );
    state = reduceDesktopFrame(
      state,
      frame("event.tool_finished", { call_id: "c1", status: "succeeded" }),
    );
    state = reduceDesktopFrame(
      state,
      frame("event.model_text_delta", { turn_id: 1, text: "结果太大，缩小范围。" }),
    );

    const assistant = state.run.timeline.find((item) => item.kind === "assistant");
    expect(assistant?.kind === "assistant" && assistant.content).toBe(
      "先查一下命令。\n\n结果太大，缩小范围。",
    );
  });

  it("does not add a leading break before the very first chunk", () => {
    let state = createDesktopTaskState("s");
    state = reduceDesktopFrame(state, frame("event.turn_started", { turn_id: 1 }));
    state = reduceDesktopFrame(
      state,
      frame("event.model_text_delta", { turn_id: 1, text: "开头。" }),
    );

    const assistant = state.run.timeline.find((item) => item.kind === "assistant");
    expect(assistant?.kind === "assistant" && assistant.content).toBe("开头。");
  });

  it("keeps consecutive deltas inside one round glued together", () => {
    // 同一轮内的流式分片是半句半句到达的，绝不能被拆段。
    let state = createDesktopTaskState("s");
    state = reduceDesktopFrame(state, frame("event.turn_started", { turn_id: 1 }));
    state = reduceDesktopFrame(state, frame("event.model_text_delta", { turn_id: 1, text: "半句" }));
    state = reduceDesktopFrame(state, frame("event.model_text_delta", { turn_id: 1, text: "话。" }));

    const assistant = state.run.timeline.find((item) => item.kind === "assistant");
    expect(assistant?.kind === "assistant" && assistant.content).toBe("半句话。");
  });

  it("lets the final content replace the streamed draft untouched", () => {
    let state = createDesktopTaskState("s");
    state = reduceDesktopFrame(state, frame("event.turn_started", { turn_id: 1 }));
    state = reduceDesktopFrame(state, frame("event.model_text_delta", { turn_id: 1, text: "过程。" }));
    state = reduceDesktopFrame(
      state,
      frame("event.tool_requested", {
        turn_id: 1,
        call_id: "c1",
        tool_name: "read_file",
        summary: "read_file a.md",
        arguments: {},
      }),
    );
    state = reduceDesktopFrame(
      state,
      frame("event.turn_finished", { turn_id: 1, content: "最终回复", duration_ms: 10 }),
    );

    const assistant = state.run.timeline.find((item) => item.kind === "assistant");
    expect(assistant?.kind === "assistant" && assistant.content).toBe("最终回复");
  });
});


describe("hydrateSession 回放过程", () => {
  it("把思考与工具调用还原成过程条目", () => {
    // 定时任务没有实时事件流，回放是唯一能看到执行过程的途径。
    const state = hydrateSession({
      sessionKey: "task:1:run:1",
      updatedAt: "2026-08-12T00:00:00Z",
      turns: [{ turnId: 1, status: "completed", errorCode: null }],
      messages: [
        { role: "user", content: "汇总昨天的项目", turnId: 1 },
        {
          role: "assistant",
          content: "",
          turnId: 1,
          reasoning: "先查一下认证状态。",
          toolCalls: ["run_command"],
        },
        { role: "tool", content: '{"exit_code":1}', turnId: 1, toolName: "run_command" },
        { role: "assistant", content: "认证未通过。", turnId: 1 },
      ],
    });

    const kinds = state.run.timeline.map((item) => item.kind);
    expect(kinds).toContain("reasoning");
    expect(kinds).toContain("tool");
    const reasoning = state.run.timeline.find((item) => item.kind === "reasoning");
    expect(reasoning && "content" in reasoning ? reasoning.content : "").toContain(
      "先查一下认证状态",
    );
  });

  it("不因为一条只调工具、没有正文的回合就丢掉它", () => {
    const state = hydrateSession({
      sessionKey: "s",
      updatedAt: "2026-08-12T00:00:00Z",
      turns: [],
      messages: [
        { role: "assistant", content: "", turnId: 1, toolCalls: ["http_get"] },
      ],
    });

    expect(state.run.timeline.length).toBeGreaterThan(0);
  });
});
