import { isPermissionMode, type ServerFrame } from "@lobster0/pi-tui/protocol";

import {
  DESKTOP_CHANNELS,
  type ApprovalDecision,
  type StartTurnInput,
} from "../common/api";
import type { BridgeService } from "./bridge-service";

const APPROVAL_DECISIONS = new Set<ApprovalDecision>(["deny", "once", "session", "always"]);

export class DesktopRequestError extends Error {
  public readonly code: string;

  public constructor(code: string, message: string) {
    super(message);
    this.name = "DesktopRequestError";
    this.code = code;
  }
}

type RegisterHandler = (
  channel: string,
  handler: (payload: unknown) => Promise<unknown>,
) => void;

export function registerDesktopIpc(
  register: RegisterHandler,
  bridge: BridgeService,
  publishFrame: (frame: ServerFrame) => void,
  chooseWorkspace: () => Promise<string | null>,
): () => void {
  register(DESKTOP_CHANNELS.bootstrap, () => bridge.start());
  register(DESKTOP_CHANNELS.taskStart, (payload) => bridge.startTurn(validateStartTurnInput(payload)));
  register(DESKTOP_CHANNELS.taskCancel, () => bridge.cancelTurn());
  register(DESKTOP_CHANNELS.approvalResolve, (payload) => {
    const input = validateApprovalInput(payload);
    return bridge.resolveApproval(input.approvalId, input.decision);
  });
  register(DESKTOP_CHANNELS.permissionsSet, (payload) => {
    const record = exactRecord(payload, ["mode"], "invalid_permission_mode");
    if (!isPermissionMode(record.mode)) {
      throw new DesktopRequestError("invalid_permission_mode", "权限模式无效");
    }
    return bridge.setPermissionMode(record.mode);
  });
  register(DESKTOP_CHANNELS.sessionsList, (payload) => {
    const input = validateSessionListInput(payload);
    return bridge.listSessions(input.limit);
  });
  register(DESKTOP_CHANNELS.sessionLoad, (payload) => {
    const input = validateHistoryInput(payload);
    return bridge.loadSession(input.sessionKey, input.limit);
  });
  register(DESKTOP_CHANNELS.automationsList, (payload) => {
    const input = validateAutomationListInput(payload);
    return bridge.listAutomations(input.limit);
  });
  register(DESKTOP_CHANNELS.workspaceChoose, async () => {
    const selected = await chooseWorkspace();
    if (selected === null) {
      return null;
    }
    return (await bridge.restartWorkspace(selected)).workspace;
  });
  return bridge.onFrame(publishFrame);
}

export function validateStartTurnInput(payload: unknown): StartTurnInput {
  const record = exactRecord(payload, ["sessionKey", "text"], "invalid_start_turn");
  if (
    !boundedString(record.sessionKey, 128)
    || !boundedString(record.text, 200_000)
    || record.text.trim().length === 0
  ) {
    throw new DesktopRequestError("invalid_start_turn", "任务内容无效");
  }
  return { sessionKey: record.sessionKey, text: record.text };
}

export function validateApprovalInput(payload: unknown): {
  approvalId: number;
  decision: ApprovalDecision;
} {
  const record = exactRecord(payload, ["approvalId", "decision"], "invalid_approval");
  if (
    !Number.isSafeInteger(record.approvalId)
    || typeof record.approvalId !== "number"
    || record.approvalId <= 0
    || typeof record.decision !== "string"
    || !APPROVAL_DECISIONS.has(record.decision as ApprovalDecision)
  ) {
    throw new DesktopRequestError("invalid_approval", "审批决定无效");
  }
  return {
    approvalId: record.approvalId,
    decision: record.decision as ApprovalDecision,
  };
}

export function validateSessionListInput(payload: unknown): { limit: number } {
  const record = exactRecord(payload, ["limit"], "invalid_session_query");
  const limit = integerBetween(record.limit, 1, 50, "invalid_session_query");
  return { limit };
}

export function validateHistoryInput(payload: unknown): {
  sessionKey: string;
  limit: number;
} {
  const record = exactRecord(payload, ["sessionKey", "limit"], "invalid_session_query");
  if (!boundedString(record.sessionKey, 256)) {
    throw new DesktopRequestError("invalid_session_query", "任务标识无效");
  }
  return {
    sessionKey: record.sessionKey,
    limit: integerBetween(record.limit, 1, 200, "invalid_session_query"),
  };
}

export function validateAutomationListInput(payload: unknown): { limit: number } {
  const record = exactRecord(payload, ["limit"], "invalid_automation_query");
  return { limit: integerBetween(record.limit, 1, 100, "invalid_automation_query") };
}

function exactRecord(
  value: unknown,
  keys: readonly string[],
  code: string,
): Record<string, unknown> {
  if (
    typeof value !== "object"
    || value === null
    || Array.isArray(value)
    || Object.keys(value).length !== keys.length
    || keys.some((key) => !Object.hasOwn(value, key))
  ) {
    throw new DesktopRequestError(code, "Desktop 请求字段无效");
  }
  return value as Record<string, unknown>;
}

function boundedString(value: unknown, maximum: number): value is string {
  return typeof value === "string" && value.length > 0 && value.length <= maximum && !value.includes("\0");
}

function integerBetween(value: unknown, minimum: number, maximum: number, code: string): number {
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value < minimum || value > maximum) {
    throw new DesktopRequestError(code, "Desktop 数量边界无效");
  }
  return value;
}
