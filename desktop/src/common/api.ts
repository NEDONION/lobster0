import type {
  JsonValue,
  PermissionMode,
  ServerFrame,
} from "@miniclaw/pi-tui/protocol";

export type ApprovalDecision = "deny" | "once" | "session" | "always";

export interface DesktopBootstrap {
  coreVersion: string;
  model: string;
  workspace: string;
  language: string;
  contextBudgetTokens: number;
  permissionMode: PermissionMode;
  tools: string[];
  capabilities: string[];
}

export interface StartTurnInput {
  sessionKey: string;
  text: string;
}

export interface SessionSummary {
  sessionKey: string;
  updatedAt: string;
  status: string;
}

export interface SessionHistory {
  sessionKey: string;
  updatedAt: string;
  turns: Record<string, JsonValue>[];
  messages: Record<string, JsonValue>[];
}

export interface AutomationSummary {
  taskId: number;
  name: string;
  status: string;
  scheduleKind: string;
  nextRunAt: string | null;
}

export interface DesktopApi {
  bootstrap(): Promise<DesktopBootstrap>;
  startTurn(input: StartTurnInput): Promise<void>;
  cancelTurn(): Promise<void>;
  resolveApproval(approvalId: number, decision: ApprovalDecision): Promise<void>;
  setPermissionMode(mode: PermissionMode): Promise<PermissionMode>;
  listSessions(limit?: number): Promise<SessionSummary[]>;
  loadSession(sessionKey: string, limit?: number): Promise<SessionHistory>;
  listAutomations(limit?: number): Promise<AutomationSummary[]>;
  chooseWorkspace(): Promise<string | null>;
  onFrame(handler: (frame: ServerFrame) => void): () => void;
}

export const DESKTOP_CHANNELS = {
  bootstrap: "desktop:bootstrap",
  taskStart: "desktop:task:start",
  taskCancel: "desktop:task:cancel",
  approvalResolve: "desktop:approval:resolve",
  permissionsSet: "desktop:permissions:set",
  sessionsList: "desktop:sessions:list",
  sessionLoad: "desktop:session:load",
  automationsList: "desktop:automations:list",
  workspaceChoose: "desktop:workspace:choose",
  frame: "desktop:frame",
} as const;

type Invoke = (channel: string, payload?: unknown) => Promise<unknown>;
type Subscribe = (
  channel: string,
  handler: (value: unknown) => void,
) => () => void;

export function createDesktopApi(invoke: Invoke, subscribe: Subscribe): DesktopApi {
  return {
    bootstrap: () => invoke(DESKTOP_CHANNELS.bootstrap) as Promise<DesktopBootstrap>,
    startTurn: (input) => invoke(DESKTOP_CHANNELS.taskStart, input) as Promise<void>,
    cancelTurn: () => invoke(DESKTOP_CHANNELS.taskCancel) as Promise<void>,
    resolveApproval: (approvalId, decision) =>
      invoke(DESKTOP_CHANNELS.approvalResolve, { approvalId, decision }) as Promise<void>,
    setPermissionMode: (mode) =>
      invoke(DESKTOP_CHANNELS.permissionsSet, { mode }) as Promise<PermissionMode>,
    listSessions: (limit = 20) =>
      invoke(DESKTOP_CHANNELS.sessionsList, { limit }) as Promise<SessionSummary[]>,
    loadSession: (sessionKey, limit = 100) =>
      invoke(DESKTOP_CHANNELS.sessionLoad, { sessionKey, limit }) as Promise<SessionHistory>,
    listAutomations: (limit = 50) =>
      invoke(DESKTOP_CHANNELS.automationsList, { limit }) as Promise<AutomationSummary[]>,
    chooseWorkspace: () =>
      invoke(DESKTOP_CHANNELS.workspaceChoose) as Promise<string | null>,
    onFrame: (handler) =>
      subscribe(DESKTOP_CHANNELS.frame, (value) => handler(value as ServerFrame)),
  };
}

declare global {
  interface Window {
    miniclaw: DesktopApi;
  }
}
