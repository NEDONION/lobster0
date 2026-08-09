import {
  BridgeClient,
  BridgeRequestError,
} from "@miniclaw/pi-tui/bridge-client";
import {
  isPermissionMode,
  type JsonValue,
  type PermissionMode,
  type ServerFrame,
} from "@miniclaw/pi-tui/protocol";

import type {
  ApprovalDecision,
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
  private readonly environment: NodeJS.ProcessEnv;
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
    this.environment = environment;
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
      const payload = await client.hello("miniclaw-desktop", "0.1.0");
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
      throw new BridgeRequestError("bridge_state", "MiniClaw Core 当前状态不允许此操作");
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
    throw new BridgeRequestError("bridge_protocol", "MiniClaw Core 握手数据无效");
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
  };
}

function stringValue(value: JsonValue | undefined): string {
  if (typeof value !== "string" || value.length === 0) {
    throw new BridgeRequestError("bridge_protocol", "MiniClaw Core 握手数据无效");
  }
  return value;
}

function stringArray(value: JsonValue | undefined): string[] {
  if (!Array.isArray(value) || value.some((item) => typeof item !== "string")) {
    throw new BridgeRequestError("bridge_protocol", "MiniClaw Core 握手数据无效");
  }
  return value as string[];
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

function protocolError(): BridgeRequestError {
  return new BridgeRequestError("bridge_protocol", "MiniClaw Core 返回了无效数据");
}
