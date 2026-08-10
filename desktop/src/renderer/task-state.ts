import type { ServerFrame } from "@lobster0/pi-tui/protocol";
import {
  appendUser,
  createInitialState,
  reduceFrame,
  type AppState,
  type Telemetry,
} from "@lobster0/pi-tui/state";

import type { SessionHistory } from "../common/api";

export type DesktopTaskStatus =
  | "idle"
  | "running"
  | "waiting_approval"
  | "completed"
  | "failed"
  | "cancelled"
  | "interrupted";

export interface DesktopTaskState {
  sessionKey: string;
  status: DesktopTaskStatus;
  run: AppState;
  error: string | null;
  /**
   * 每个已完成回合的 telemetry 快照，按 turnId 索引。
   *
   * `AppState.telemetry` 只保留最近一次运行的数字，会被下一回合覆盖；
   * 而界面要在每条回复下方显示"这一条"的耗时与用量，因此在回合结束时
   * 把当时的读数固定下来。失败或取消的回合不记录——那些数字不完整，
   * 展示出来会误导。
   */
  turnTelemetry: Readonly<Record<number, Telemetry>>;
}

export function createDesktopTaskState(sessionKey: string): DesktopTaskState {
  return {
    sessionKey,
    status: "idle",
    run: createInitialState(),
    error: null,
    turnTelemetry: {},
  };
}

export function appendDesktopUser(
  state: DesktopTaskState,
  content: string,
): DesktopTaskState {
  return { ...state, run: appendUser(state.run, content), error: null };
}

export function continueDesktopApproval(state: DesktopTaskState): DesktopTaskState {
  return {
    ...state,
    status: "running",
    run: { ...state.run, busy: true, pendingApproval: null },
    error: null,
  };
}

export function cancelDesktopTask(state: DesktopTaskState): DesktopTaskState {
  return { ...state, status: "cancelled", run: terminal(state.run), error: null };
}

export function hydrateSession(history: SessionHistory): DesktopTaskState {
  let state = createDesktopTaskState(history.sessionKey);
  for (const message of history.messages) {
    if (message.role === "user") {
      state = appendDesktopUser(state, message.content);
    } else {
      state = {
        ...state,
        run: reduceFrame(state.run, {
          v: 1,
          type: "event.turn_finished",
          payload: { turn_id: message.turnId ?? 0, content: message.content },
        }),
      };
    }
  }
  const latest = history.turns.at(-1);
  if (!latest) {
    return state;
  }
  if (latest.status === "failed" && latest.errorCode === "runtime_interrupted") {
    return { ...state, status: "interrupted", error: "上次运行意外中断" };
  }
  if (latest.status === "failed") {
    return {
      ...state,
      status: "failed",
      error: `本轮失败：${stableCode(latest.errorCode, "agent")}`,
    };
  }
  if (latest.status === "queued" || latest.status === "running") {
    return { ...state, status: "running" };
  }
  if (latest.status === "waiting_approval") {
    return { ...state, status: "waiting_approval" };
  }
  if (latest.status === "cancelled") {
    return { ...state, status: "cancelled" };
  }
  return { ...state, status: latest.status === "completed" ? "completed" : "idle" };
}

export function reduceDesktopFrame(
  state: DesktopTaskState,
  frame: ServerFrame,
): DesktopTaskState {
  // 折叠状态不再挂在逐条 item 上：Desktop 把连续的思考与工具聚合成「过程」块，
  // 由渲染层维护块级折叠，因此这里不需要再干预 pi-tui 的 expanded 默认值。
  const reduced = reduceFrame(state.run, frame);
  if (frame.type === "event.turn_started") {
    return { ...state, status: "running", run: reduced, error: null };
  }
  if (frame.type === "event.approval_required") {
    return { ...state, status: "waiting_approval", run: reduced, error: null };
  }
  if (frame.type === "event.turn_finished") {
    const turnId = frame.payload.turn_id;
    return {
      ...state,
      status: "completed",
      run: reduced,
      error: null,
      turnTelemetry:
        typeof turnId === "number"
          ? { ...state.turnTelemetry, [turnId]: reduced.telemetry }
          : state.turnTelemetry,
    };
  }
  if (frame.type === "event.turn_failed") {
    return {
      ...state,
      status: "failed",
      run: terminal(reduced),
      error: `本轮失败：${stableCode(frame.payload.error_code, "agent")}`,
    };
  }
  if (frame.type === "event.turn_cancelled") {
    return { ...state, status: "cancelled", run: terminal(reduced), error: null };
  }
  if (frame.type === "event.bridge_error") {
    return {
      ...state,
      status: "failed",
      run: terminal(reduced),
      error: `Core 操作失败：${stableCode(frame.payload.code, "core_operation_failed")}`,
    };
  }
  return { ...state, run: reduced };
}

function terminal(state: AppState): AppState {
  return { ...state, busy: false, activeTurnId: null, pendingApproval: null };
}

function stableCode(value: unknown, fallback: string): string {
  return typeof value === "string" && /^[a-z0-9_.-]{1,64}$/.test(value) ? value : fallback;
}
