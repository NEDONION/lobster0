import {
  BridgeClient,
  BridgeRequestError,
} from "@lobster0/pi-tui/bridge-client";
import {
  isPermissionMode,
  type JsonValue,
  type PermissionMode,
  type ServerFrame,
} from "@lobster0/pi-tui/protocol";
import { isAbsolute } from "node:path";

import type {
  ApprovalDecision,
  AutomationCreateInput,
  AutomationList,
  AutomationRun,
  AutomationSummary,
  DesktopBootstrap,
  SessionHistory,
  SessionSummary,
  StartTurnInput,
} from "../common/api";

type BridgeStatus =
  | "starting"
  | "idle"
  | "running"
  | "waiting_approval"
  | "stopped"
  | "failed";

type BridgePort = Pick<
  BridgeClient,
  | "hello"
  | "request"
  | "onEvent"
  | "onFatal"
  | "startTurn"
  | "cancelTurn"
  | "resolveApproval"
  | "setPermissionMode"
  | "shutdown"
  | "kill"
>;

type ClientFactory = (environment: NodeJS.ProcessEnv) => BridgePort;

export class BridgeService {
  private readonly createClient: ClientFactory;
  private environment: NodeJS.ProcessEnv;
  private readonly frameHandlers = new Set<(frame: ServerFrame) => void>();
  private client: BridgePort | null = null;
  private bootstrapData: DesktopBootstrap | null = null;
  private startPromise: Promise<DesktopBootstrap> | null = null;
  private removeEventHandler: (() => void) | null = null;
  private removeFatalHandler: (() => void) | null = null;
  private currentStatus: BridgeStatus = "stopped";

  public constructor(
    createClient: ClientFactory = BridgeClient.spawnFromEnvironment,
    environment: NodeJS.ProcessEnv = process.env,
  ) {
    this.createClient = createClient;
    this.environment = { ...environment };
  }

  public get status(): BridgeStatus {
    return this.currentStatus;
  }

  public start(): Promise<DesktopBootstrap> {
    if (this.bootstrapData && this.currentStatus !== "failed" && this.currentStatus !== "stopped") {
      return Promise.resolve(this.bootstrapData);
    }
    if (this.startPromise) {
      return this.startPromise;
    }
    this.currentStatus = "starting";
    this.startPromise = this.connect().finally(() => {
      this.startPromise = null;
    });
    return this.startPromise;
  }

  public onFrame(handler: (frame: ServerFrame) => void): () => void {
    this.frameHandlers.add(handler);
    return () => this.frameHandlers.delete(handler);
  }

  public async startTurn(input: StartTurnInput): Promise<void> {
    const client = this.requireClient("idle");
    this.currentStatus = "running";
    try {
      await client.startTurn(input.sessionKey, input.text);
    } catch (error) {
      this.currentStatus = "idle";
      throw error;
    }
  }

  public async cancelTurn(): Promise<void> {
    if (this.currentStatus !== "running" && this.currentStatus !== "waiting_approval") {
      throw new BridgeRequestError("turn_not_active", "当前没有运行中的任务");
    }
    const client = this.requireClient();
    await client.cancelTurn();
    this.currentStatus = "idle";
  }

  public async resolveApproval(
    approvalId: number,
    decision: ApprovalDecision,
  ): Promise<void> {
    const client = this.requireClient("waiting_approval");
    this.currentStatus = "running";
    try {
      await client.resolveApproval(approvalId, decision);
    } catch (error) {
      this.currentStatus = "waiting_approval";
      throw error;
    }
  }

  public async setPermissionMode(mode: PermissionMode): Promise<PermissionMode> {
    const client = this.requireClient("idle");
    const selected = await client.setPermissionMode(mode);
    if (this.bootstrapData) {
      this.bootstrapData = { ...this.bootstrapData, permissionMode: selected };
    }
    return selected;
  }

  public async listSessions(limit: number): Promise<SessionSummary[]> {
    const response = await this.requireClient().request("session.list", { limit });
    const sessions = response.sessions;
    if (!Array.isArray(sessions)) {
      throw protocolError();
    }
    return sessions.map((value) => {
      const record = recordValue(value);
      return {
        sessionKey: stringValue(record.session_key),
        title: stringValue(record.title),
        updatedAt: stringValue(record.updated_at),
        status: stringValue(record.status),
      };
    });
  }

  public async loadSession(
    sessionKey: string,
    limit: number,
  ): Promise<SessionHistory> {
    const response = await this.requireClient().request("session.history", {
      session_key: sessionKey,
      limit,
    });
    const turns = response.turns;
    const messages = response.messages;
    if (!Array.isArray(turns) || !Array.isArray(messages)) {
      throw protocolError();
    }
    return {
      sessionKey: stringValue(response.session_key),
      updatedAt: stringValue(response.updated_at),
      turns: turns.map((value) => {
        const record = recordValue(value);
        return {
          turnId: positiveInteger(record.turn_id),
          status: stringValue(record.status),
          errorCode: nullableString(record.error_code),
        };
      }),
      messages: messages.map((value) => {
        const record = recordValue(value);
        const role = record.role;
        if (role !== "user" && role !== "assistant") {
          throw protocolError();
        }
        return {
          role,
          content: stringValue(record.content),
          turnId: nullablePositiveInteger(record.turn_id),
        };
      }),
    };
  }

  public async listAutomations(limit: number): Promise<AutomationList> {
    const response = await this.requireClient().request("automation.list", { limit });
    if (typeof response.enabled !== "boolean" || !Array.isArray(response.tasks)) {
      throw protocolError();
    }
    return {
      enabled: response.enabled,
      tasks: response.tasks.map((value) => automationTask(recordValue(value))),
    };
  }

  public async pauseAutomation(taskId: number): Promise<AutomationSummary> {
    return this.mutateAutomationTask("automation.pause", taskId);
  }

  public async resumeAutomation(taskId: number): Promise<AutomationSummary> {
    return this.mutateAutomationTask("automation.resume", taskId);
  }

  public async cancelAutomation(taskId: number): Promise<AutomationSummary> {
    return this.mutateAutomationTask("automation.cancel", taskId);
  }

  public async runAutomation(taskId: number): Promise<AutomationRun> {
    const response = await this.requireClient().request("automation.run", {
      task_id: assertTaskId(taskId),
    });
    return automationRun(recordValue(requiredValue(response.run)));
  }

  public async listAutomationRuns(taskId: number, limit: number): Promise<AutomationRun[]> {
    const response = await this.requireClient().request("automation.runs", {
      task_id: assertTaskId(taskId),
      limit,
    });
    if (!Array.isArray(response.runs)) {
      throw protocolError();
    }
    return response.runs.map((value) => automationRun(recordValue(value)));
  }

  public async haltAutomation(reason: string): Promise<void> {
    // Core 侧 reason 必填且非空白；这里先挡一次，避免把明显无效的请求发出去。
    if (reason.trim().length === 0) {
      throw new BridgeRequestError("bridge_state", "急停必须填写原因");
    }
    await this.requireClient().request("automation.halt", { reason });
  }

  public async unhaltAutomation(): Promise<void> {
    await this.requireClient().request("automation.unhalt", {});
  }

  public async createAutomation(input: AutomationCreateInput): Promise<AutomationSummary> {
    const schedule: Record<string, string> = {
      kind: input.scheduleKind,
      expression: input.expression,
    };
    if (input.timezone) {
      schedule.timezone = input.timezone;
    }
    const response = await this.requireClient().request("automation.create", {
      name: input.name,
      prompt: input.prompt,
      schedule,
    });
    return automationTask(recordValue(requiredValue(response.task)));
  }

  /** pause/resume/cancel 的响应形状一致，共用同一条校验路径。 */
  private async mutateAutomationTask(
    requestType: "automation.pause" | "automation.resume" | "automation.cancel",
    taskId: number,
  ): Promise<AutomationSummary> {
    const response = await this.requireClient().request(requestType, {
      task_id: assertTaskId(taskId),
    });
    return automationTask(recordValue(requiredValue(response.task)));
  }

  public async restartWorkspace(workspace: string): Promise<DesktopBootstrap> {
    if (
      this.currentStatus !== "idle"
      || !isAbsolute(workspace)
      || workspace.length > 4_096
      || workspace.includes("\0")
    ) {
      throw new BridgeRequestError("bridge_state", "Lobster0 Core 当前状态不允许切换工作目录");
    }
    const previousEnvironment = this.environment;
    await this.stop();
    this.environment = { ...previousEnvironment, LOBSTER0_WORKSPACE: workspace };
    try {
      return await this.start();
    } catch (error) {
      await this.stop();
      this.environment = previousEnvironment;
      await this.start().catch(() => undefined);
      throw error;
    }
  }

  public async stop(): Promise<void> {
    if (this.startPromise) {
      await this.startPromise.catch(() => undefined);
    }
    const client = this.client;
    this.clearClient();
    this.bootstrapData = null;
    this.currentStatus = "stopped";
    if (client) {
      await client.shutdown().catch(() => client.kill());
    }
  }

  private async connect(): Promise<DesktopBootstrap> {
    const client = this.createClient(this.environment);
    this.client = client;
    this.removeEventHandler = client.onEvent((frame) => this.handleFrame(frame));
    this.removeFatalHandler = client.onFatal(() => {
      this.currentStatus = "failed";
    });
    try {
      const payload = await client.hello("lobster0-desktop", "0.1.0");
      const bootstrap = parseBootstrap(payload);
      this.bootstrapData = bootstrap;
      this.currentStatus = "idle";
      return bootstrap;
    } catch (error) {
      this.currentStatus = "failed";
      this.clearClient();
      client.kill();
      throw error;
    }
  }

  private handleFrame(frame: ServerFrame): void {
    if (frame.type === "event.turn_started") {
      this.currentStatus = "running";
    } else if (frame.type === "event.approval_required") {
      this.currentStatus = "waiting_approval";
    } else if (
      frame.type === "event.turn_finished"
      || frame.type === "event.turn_failed"
      || frame.type === "event.turn_cancelled"
      || frame.type === "event.bridge_error"
    ) {
      this.currentStatus = "idle";
    }
    for (const handler of this.frameHandlers) {
      handler(frame);
    }
  }

  private requireClient(expectedStatus?: BridgeStatus): BridgePort {
    if (!this.client || (expectedStatus && this.currentStatus !== expectedStatus)) {
      throw new BridgeRequestError("bridge_state", "Lobster0 Core 当前状态不允许此操作");
    }
    return this.client;
  }

  private clearClient(): void {
    this.removeEventHandler?.();
    this.removeFatalHandler?.();
    this.removeEventHandler = null;
    this.removeFatalHandler = null;
    this.client = null;
  }
}

function parseBootstrap(payload: Record<string, JsonValue>): DesktopBootstrap {
  const permissionMode = payload.permission_mode;
  if (
    payload.protocol !== 1
    || !isPermissionMode(permissionMode)
    || typeof payload.context_budget_tokens !== "number"
    || !Number.isSafeInteger(payload.context_budget_tokens)
    || payload.context_budget_tokens <= 0
  ) {
    throw new BridgeRequestError("bridge_protocol", "Lobster0 Core 握手数据无效");
  }
  return {
    coreVersion: stringValue(payload.core_version),
    model: stringValue(payload.model),
    workspace: stringValue(payload.workspace),
    language: stringValue(payload.language),
    contextBudgetTokens: payload.context_budget_tokens,
    permissionMode,
    tools: stringArray(payload.tools),
    capabilities: stringArray(payload.capabilities),
    automationEnabled: booleanValue(payload.automation_enabled),
  };
}

function stringValue(value: JsonValue | undefined): string {
  if (typeof value !== "string" || value.length === 0) {
    throw new BridgeRequestError("bridge_protocol", "Lobster0 Core 握手数据无效");
  }
  return value;
}

function stringArray(value: JsonValue | undefined): string[] {
  if (!Array.isArray(value) || value.some((item) => typeof item !== "string")) {
    throw new BridgeRequestError("bridge_protocol", "Lobster0 Core 握手数据无效");
  }
  return value as string[];
}

function booleanValue(value: JsonValue | undefined): boolean {
  if (typeof value !== "boolean") {
    throw protocolError();
  }
  return value;
}

function recordValue(value: JsonValue): Record<string, JsonValue> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw protocolError();
  }
  return value;
}

function positiveInteger(value: JsonValue | undefined): number {
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value <= 0) {
    throw protocolError();
  }
  return value;
}

function nullablePositiveInteger(value: JsonValue | undefined): number | null {
  return value === null ? null : positiveInteger(value);
}

function nullableString(value: JsonValue | undefined): string | null {
  if (value === null) {
    return null;
  }
  return stringValue(value);
}

/** 把一条 Task 响应投影成 Renderer 类型；与只读列表共用，避免字段集漂移。 */
function automationTask(record: Record<string, JsonValue>): AutomationSummary {
  return {
    taskId: positiveInteger(record.task_id),
    name: stringValue(record.name),
    status: stringValue(record.status),
    scheduleKind: stringValue(record.schedule_kind),
    nextRunAt: nullableString(record.next_run_at),
  };
}

/** 把一条运行记录投影成 Renderer 类型。 */
function automationRun(record: Record<string, JsonValue>): AutomationRun {
  return {
    runId: positiveInteger(record.run_id),
    taskId: positiveInteger(record.task_id),
    status: stringValue(record.status),
    scheduledFor: stringValue(record.scheduled_for),
    errorCode: nullableString(record.error_code),
  };
}

/**
 * 在发请求前挡住非法 task_id。
 *
 * Core 侧 protocol 会再校验一次——这里只是让明显错误在本地就失败，
 * 不是把校验责任前移。
 */
function assertTaskId(taskId: number): number {
  if (!Number.isSafeInteger(taskId) || taskId <= 0) {
    throw new BridgeRequestError("bridge_state", "定时任务 ID 不合法");
  }
  return taskId;
}

/** 响应里缺少必需字段时，按协议错误处理而不是让 undefined 继续流动。 */
function requiredValue(value: JsonValue | undefined): JsonValue {
  if (value === undefined) {
    throw protocolError();
  }
  return value;
}

function protocolError(): BridgeRequestError {
  return new BridgeRequestError("bridge_protocol", "Lobster0 Core 返回了无效数据");
}
