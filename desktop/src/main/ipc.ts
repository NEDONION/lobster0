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
  chooseAttachment: () => Promise<string | null>,
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
  register(DESKTOP_CHANNELS.coreRestart, () => bridge.restartCore());
  register(DESKTOP_CHANNELS.subagentsList, () => bridge.listSubagents());
  register(DESKTOP_CHANNELS.artifactsList, (payload) => {
    const input = validateArtifactListInput(payload);
    return bridge.listArtifacts(input.sessionKey, input.limit);
  });
  register(DESKTOP_CHANNELS.artifactPreview, (payload) => {
    const input = validateArtifactPreviewInput(payload);
    return bridge.previewArtifact(input.artifactId, input.maxBytes);
  });
  register(DESKTOP_CHANNELS.artifactReveal, (payload) =>
    bridge.revealArtifact(validateArtifactIdInput(payload).artifactId));
  register(DESKTOP_CHANNELS.attachmentPick, () => chooseAttachment());
  register(DESKTOP_CHANNELS.attachmentStage, (payload) => {
    const input = validateAttachmentPathInput(payload);
    return bridge.stageAttachment(input.path, input.declaredMediaType);
  });
  register(DESKTOP_CHANNELS.providersList, () => bridge.listProviders());
  register(DESKTOP_CHANNELS.providerUpsert, (payload) =>
    bridge.upsertProvider(validateProviderUpsertInput(payload)));
  register(DESKTOP_CHANNELS.providerRemove, (payload) =>
    bridge.removeProvider(validateProviderIdInput(payload).id));
  register(DESKTOP_CHANNELS.providerSelect, (payload) => {
    const input = validateProviderSelectInput(payload);
    return bridge.selectProvider(input.id, input.model);
  });
  register(DESKTOP_CHANNELS.providerSecret, (payload) => {
    const input = validateProviderSecretInput(payload);
    return bridge.setProviderSecret(input.id, input.value);
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
  const keys = Object.hasOwn(payload ?? {}, "attachmentIds")
    ? ["sessionKey", "text", "attachmentIds"]
    : ["sessionKey", "text"];
  const record = exactRecord(payload, keys, "invalid_start_turn");
  if (
    !boundedString(record.sessionKey, 128)
    || !boundedString(record.text, 200_000)
    || record.text.trim().length === 0
  ) {
    throw new DesktopRequestError("invalid_start_turn", "任务内容无效");
  }
  const input: StartTurnInput = { sessionKey: record.sessionKey, text: record.text };
  if (Object.hasOwn(record, "attachmentIds")) {
    const ids = record.attachmentIds;
    if (
      !Array.isArray(ids)
      || ids.length < 1
      || ids.length > MAX_ATTACHMENTS
      || !ids.every((id) => typeof id === "string" && ARTIFACT_ID.test(id))
    ) {
      throw new DesktopRequestError("invalid_start_turn", "附件引用无效");
    }
    input.attachmentIds = ids;
  }
  return input;
}

// Core 的 _MEDIA_EXTENSIONS 白名单，提前在 Main 层拒绝以给出更快的反馈；
// 真正的判定仍在 Core 的 magic byte 嗅探里，这里的映射不被信任。
const ATTACHMENT_MEDIA_TYPES: Record<string, string> = {
  ".txt": "text/plain",
  ".csv": "text/csv",
  ".json": "application/json",
  ".pdf": "application/pdf",
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".zip": "application/zip",
};
const ARTIFACT_ID = /^art_[0-9a-f]{64}$/;
const MAX_ATTACHMENTS = 10;

export function validateAttachmentPathInput(payload: unknown): {
  path: string;
  declaredMediaType: string;
} {
  // declaredMediaType 刻意不在字段集里：类型只能由扩展名推导，否则调用方
  // 可以谎报类型绕过这一层。
  const record = exactRecord(payload, ["path"], "invalid_attachment");
  const path = record.path;
  if (!boundedString(path, 4096) || !path.startsWith("/")) {
    throw new DesktopRequestError("invalid_attachment", "附件路径无效");
  }
  const dot = path.lastIndexOf(".");
  const slash = path.lastIndexOf("/");
  const extension = dot > slash ? path.slice(dot).toLowerCase() : "";
  const declaredMediaType = ATTACHMENT_MEDIA_TYPES[extension];
  if (declaredMediaType === undefined) {
    throw new DesktopRequestError("invalid_attachment", "暂不支持该文件类型");
  }
  return { path, declaredMediaType };
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

// 与 Core 的 config._PROVIDER_ID 保持同一套字符集：它要参与密钥环境变量名的
// 推导，任何越界字符都可能变成写入其他环境变量的手段。
const PROVIDER_ID = /^[a-z0-9][a-z0-9_-]{0,31}$/;
const PROVIDER_CODE = "invalid_provider_action";

export function validateProviderIdInput(payload: unknown): { id: string } {
  const record = exactRecord(payload, ["id"], PROVIDER_CODE);
  return { id: providerId(record.id) };
}

export function validateProviderUpsertInput(payload: unknown): {
  id: string;
  baseUrl: string;
  timeoutSeconds: number;
} {
  // apiKeyEnv 刻意不在字段集里，exactRecord 会因多出一个键而整体拒绝。
  const record = exactRecord(payload, ["id", "baseUrl", "timeoutSeconds"], PROVIDER_CODE);
  const baseUrl = record.baseUrl;
  if (!boundedString(baseUrl, 500) || !isHttpUrl(baseUrl)) {
    throw new DesktopRequestError(PROVIDER_CODE, "Provider 地址必须是 http(s) URL");
  }
  return {
    id: providerId(record.id),
    baseUrl,
    timeoutSeconds: integerBetween(record.timeoutSeconds, 1, 3600, PROVIDER_CODE),
  };
}

export function validateProviderSelectInput(payload: unknown): { id: string; model: string } {
  const record = exactRecord(payload, ["id", "model"], PROVIDER_CODE);
  const model = record.model;
  if (!boundedString(model, 200) || model.trim().length === 0) {
    throw new DesktopRequestError(PROVIDER_CODE, "模型名不能为空");
  }
  return { id: providerId(record.id), model };
}

export function validateProviderSecretInput(payload: unknown): { id: string; value: string } {
  const record = exactRecord(payload, ["id", "value"], PROVIDER_CODE);
  const value = record.value;
  // 边缘空白与任何换行都会破坏 dotenv 或注入第二个变量；值本身不做 trim 后转发，
  // 因为密钥必须逐字节保真，只做合法性判定。
  if (
    !boundedString(value, 4096)
    || value.trim().length === 0
    || value !== value.trim()
    || /[\r\n\u2028\u2029]/.test(value)
  ) {
    throw new DesktopRequestError(PROVIDER_CODE, "密钥值不合法");
  }
  return { id: providerId(record.id), value };
}

const ARTIFACT_CODE = "invalid_artifact_query";

export function validateArtifactListInput(payload: unknown): {
  sessionKey: string;
  limit: number;
} {
  const record = exactRecord(payload, ["sessionKey", "limit"], ARTIFACT_CODE);
  if (!boundedString(record.sessionKey, 128)) {
    throw new DesktopRequestError(ARTIFACT_CODE, "会话标识无效");
  }
  return {
    sessionKey: record.sessionKey,
    limit: integerBetween(record.limit, 1, 500, ARTIFACT_CODE),
  };
}

export function validateArtifactIdInput(payload: unknown): { artifactId: string } {
  // 只收 id：路径必须由 Core 从 id 解析，接受调用方给路径等于开放任意本地
  // 路径的「在访达中显示」。
  const record = exactRecord(payload, ["artifactId"], ARTIFACT_CODE);
  return { artifactId: artifactId(record.artifactId) };
}

export function validateArtifactPreviewInput(payload: unknown): {
  artifactId: string;
  maxBytes: number;
} {
  const record = exactRecord(payload, ["artifactId", "maxBytes"], ARTIFACT_CODE);
  return {
    artifactId: artifactId(record.artifactId),
    maxBytes: integerBetween(record.maxBytes, 1, 1_048_576, ARTIFACT_CODE),
  };
}

function artifactId(value: unknown): string {
  if (typeof value !== "string" || !ARTIFACT_ID.test(value)) {
    throw new DesktopRequestError(ARTIFACT_CODE, "产物标识无效");
  }
  return value;
}

function providerId(value: unknown): string {
  if (typeof value !== "string" || !PROVIDER_ID.test(value)) {
    throw new DesktopRequestError(PROVIDER_CODE, "Provider 标识不合法");
  }
  return value;
}

function isHttpUrl(value: string): boolean {
  try {
    return ["http:", "https:"].includes(new URL(value).protocol);
  } catch {
    return false;
  }
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
