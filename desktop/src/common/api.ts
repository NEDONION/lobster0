import type {
  PermissionMode,
  ServerFrame,
} from "@lobster0/pi-tui/protocol";

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
  automationEnabled: boolean;
}

export interface StartTurnInput {
  sessionKey: string;
  text: string;
}

export interface SessionSummary {
  sessionKey: string;
  title: string;
  updatedAt: string;
  status: string;
}

export interface SessionTurn {
  turnId: number;
  status: string;
  errorCode: string | null;
}

export interface SessionMessage {
  role: "user" | "assistant";
  content: string;
  turnId: number | null;
}

export interface SessionHistory {
  sessionKey: string;
  updatedAt: string;
  turns: SessionTurn[];
  messages: SessionMessage[];
}

export interface AutomationSummary {
  taskId: number;
  name: string;
  status: string;
  scheduleKind: string;
  scheduleExpression: string;
  nextRunAt: string | null;
}

export interface AutomationRun {
  runId: number;
  taskId: number;
  status: string;
  scheduledFor: string;
  errorCode: string | null;
}

export interface AutomationCreateInput {
  name: string;
  prompt: string;
  scheduleKind: "once" | "interval" | "cron";
  expression: string;
  timezone?: string;
}

export interface AutomationList {
  enabled: boolean;
  tasks: AutomationSummary[];
}

export interface DesktopApi {
  bootstrap(): Promise<DesktopBootstrap>;
  startTurn(input: StartTurnInput): Promise<void>;
  cancelTurn(): Promise<void>;
  resolveApproval(approvalId: number, decision: ApprovalDecision): Promise<void>;
  setPermissionMode(mode: PermissionMode): Promise<PermissionMode>;
  listSessions(limit?: number): Promise<SessionSummary[]>;
  loadSession(sessionKey: string, limit?: number): Promise<SessionHistory>;
  listAutomations(limit?: number): Promise<AutomationList>;
  chooseWorkspace(): Promise<string | null>;
  pauseAutomation(taskId: number): Promise<AutomationSummary>;
  resumeAutomation(taskId: number): Promise<AutomationSummary>;
  cancelAutomation(taskId: number): Promise<AutomationSummary>;
  runAutomation(taskId: number): Promise<AutomationRun>;
  listAutomationRuns(taskId: number, limit?: number): Promise<AutomationRun[]>;
  haltAutomation(reason: string): Promise<void>;
  unhaltAutomation(): Promise<void>;
  createAutomation(input: AutomationCreateInput): Promise<AutomationSummary>;
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
  automationPause: "desktop:automations:pause",
  automationResume: "desktop:automations:resume",
  automationCancel: "desktop:automations:cancel",
  automationRun: "desktop:automations:run",
  automationRuns: "desktop:automations:runs",
  automationHalt: "desktop:automations:halt",
  automationUnhalt: "desktop:automations:unhalt",
  automationCreate: "desktop:automations:create",
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
      invoke(DESKTOP_CHANNELS.automationsList, { limit }) as Promise<AutomationList>,
    chooseWorkspace: () =>
      invoke(DESKTOP_CHANNELS.workspaceChoose) as Promise<string | null>,
    pauseAutomation: (taskId) =>
      invoke(DESKTOP_CHANNELS.automationPause, { taskId }) as Promise<AutomationSummary>,
    resumeAutomation: (taskId) =>
      invoke(DESKTOP_CHANNELS.automationResume, { taskId }) as Promise<AutomationSummary>,
    cancelAutomation: (taskId) =>
      invoke(DESKTOP_CHANNELS.automationCancel, { taskId }) as Promise<AutomationSummary>,
    runAutomation: (taskId) =>
      invoke(DESKTOP_CHANNELS.automationRun, { taskId }) as Promise<AutomationRun>,
    listAutomationRuns: (taskId, limit = 20) =>
      invoke(DESKTOP_CHANNELS.automationRuns, { taskId, limit }) as Promise<AutomationRun[]>,
    haltAutomation: (reason) =>
      invoke(DESKTOP_CHANNELS.automationHalt, { reason }) as Promise<void>,
    unhaltAutomation: () => invoke(DESKTOP_CHANNELS.automationUnhalt) as Promise<void>,
    createAutomation: (input) =>
      invoke(DESKTOP_CHANNELS.automationCreate, input) as Promise<AutomationSummary>,
    onFrame: (handler) =>
      subscribe(DESKTOP_CHANNELS.frame, (value) => handler(value as ServerFrame)),
  };
}

declare global {
  interface Window {
    lobster0: DesktopApi;
  }
}
