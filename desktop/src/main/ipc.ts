import { isPermissionMode, type ServerFrame } from "@lobster0/pi-tui/protocol";

import {
  DESKTOP_CHANNELS,
  type ApprovalDecision,
  type AutomationCreateInput,
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
  register(DESKTOP_CHANNELS.automationPause, (payload) =>
    bridge.pauseAutomation(validateTaskIdInput(payload).taskId));
  register(DESKTOP_CHANNELS.automationResume, (payload) =>
    bridge.resumeAutomation(validateTaskIdInput(payload).taskId));
  register(DESKTOP_CHANNELS.automationCancel, (payload) =>
    bridge.cancelAutomation(validateTaskIdInput(payload).taskId));
  register(DESKTOP_CHANNELS.automationRun, (payload) =>
    bridge.runAutomation(validateTaskIdInput(payload).taskId));
  register(DESKTOP_CHANNELS.automationRuns, (payload) => {
    const input = validateAutomationRunsInput(payload);
    return bridge.listAutomationRuns(input.taskId, input.limit);
  });
  register(DESKTOP_CHANNELS.automationHalt, (payload) =>
    bridge.haltAutomation(validateHaltInput(payload).reason));
  register(DESKTOP_CHANNELS.automationUnhalt, () => bridge.unhaltAutomation());
  register(DESKTOP_CHANNELS.automationCreate, (payload) =>
    bridge.createAutomation(validateAutomationCreateInput(payload)));
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

export function validateTaskIdInput(payload: unknown): { taskId: number } {
  const record = exactRecord(payload, ["taskId"], "invalid_automation_action");
  return {
    taskId: integerBetween(record.taskId, 1, 2_147_483_647, "invalid_automation_action"),
  };
}

export function validateAutomationRunsInput(
  payload: unknown,
): { taskId: number; limit: number } {
  const record = exactRecord(payload, ["taskId", "limit"], "invalid_automation_action");
  return {
    taskId: integerBetween(record.taskId, 1, 2_147_483_647, "invalid_automation_action"),
    limit: integerBetween(record.limit, 1, 100, "invalid_automation_action"),
  };
}

export function validateHaltInput(payload: unknown): { reason: string } {
  const record = exactRecord(payload, ["reason"], "invalid_automation_action");
  if (!boundedString(record.reason, 500) || record.reason.trim().length === 0) {
    throw new DesktopRequestError("invalid_automation_action", "急停原因不能为空");
  }
  return { reason: record.reason };
}

/**
 * 校验创建定时任务的输入。
 *
 * `timezone` 是唯一可选字段，因此不能用 exactRecord 的固定键集，改为先查未知键
 * 再逐字段校验。interval 的 5 分钟下限在这里也挡一次——Core 侧还会再校验，
 * 只在一端做等于没做。
 */
export function validateAutomationCreateInput(payload: unknown): AutomationCreateInput {
  if (typeof payload !== "object" || payload === null || Array.isArray(payload)) {
    throw new DesktopRequestError("invalid_automation_action", "创建定时任务的参数不合法");
  }
  const record = payload as Record<string, unknown>;
  const allowed = new Set(["name", "prompt", "scheduleKind", "expression", "timezone"]);
  for (const key of Object.keys(record)) {
    if (!allowed.has(key)) {
      throw new DesktopRequestError("invalid_automation_action", "创建定时任务的参数不合法");
    }
  }
  const kind = record.scheduleKind;
  if (kind !== "once" && kind !== "interval" && kind !== "cron") {
    throw new DesktopRequestError("invalid_automation_action", "不支持的调度类型");
  }
  if (
    !boundedString(record.name, 64)
    || !boundedString(record.prompt, 4_000)
    || !boundedString(record.expression, 200)
    || record.name.trim().length === 0
    || record.prompt.trim().length === 0
    || record.expression.trim().length === 0
  ) {
    throw new DesktopRequestError("invalid_automation_action", "创建定时任务的参数不合法");
  }
  const { name, prompt, expression } = record;
  if (kind === "interval") {
    const seconds = Number(expression.trim());
    if (!Number.isInteger(seconds) || seconds < 300) {
      throw new DesktopRequestError("invalid_automation_action", "间隔不能短于 5 分钟");
    }
  }
  const input: AutomationCreateInput = {
    name,
    prompt,
    scheduleKind: kind,
    expression,
  };
  if (record.timezone !== undefined) {
    if (!boundedString(record.timezone, 64)) {
      throw new DesktopRequestError("invalid_automation_action", "时区不合法");
    }
    input.timezone = record.timezone;
  }
  return input;
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
