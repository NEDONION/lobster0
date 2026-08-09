import type { ServerFrame } from "@miniclaw/pi-tui/protocol";
import {
  appendUser,
  createInitialState,
  reduceFrame,
  type AppState,
} from "@miniclaw/pi-tui/state";

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
}

export function createDesktopTaskState(sessionKey: string): DesktopTaskState {
  return {
    sessionKey,
    status: "idle",
    run: createInitialState(),
    error: null,
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
  const reduced = reduceFrame(state.run, frame);
  if (frame.type === "event.turn_started") {
    return { ...state, status: "running", run: reduced, error: null };
  }
  if (frame.type === "event.approval_required") {
    return { ...state, status: "waiting_approval", run: reduced, error: null };
  }
  if (frame.type === "event.turn_finished") {
    return { ...state, status: "completed", run: reduced, error: null };
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
