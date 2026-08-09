import type { ServerFrame } from "@miniclaw/pi-tui/protocol";
import {
  appendUser,
  createInitialState,
  reduceFrame,
  type AppState,
} from "@miniclaw/pi-tui/state";

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
