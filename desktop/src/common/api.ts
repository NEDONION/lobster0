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
  /** 已 stage 的附件；不带附件时该字段整体缺席。 */
  attachmentIds?: string[];
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
  role: "user" | "assistant" | "tool";
  content: string;
  turnId: number | null;
  /** Assistant 的思考正文；模型没给时缺席。 */
  reasoning?: string;
  /** 该条 Assistant 发起的工具名，顺序与模型返回一致。 */
  toolCalls?: string[];
  /** 工具结果对应的工具名，由 Core 从 call_id 关联。 */
  toolName?: string | null;
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
  startedAt: string | null;
  completedAt: string | null;
  errorCode: string | null;
  resultPreview: string | null;
  inputTokens: number | null;
  outputTokens: number | null;
  /** 用来打开那次运行的完整过程。 */
  sessionKey: string | null;
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

export interface AttachmentRef {
  artifactId: string;
  filename: string;
  mediaType: string;
  sizeBytes: number;
}

export interface ArtifactSummary {
  artifactId: string;
  filename: string | null;
  mediaType: string;
  sizeBytes: number;
  origin: string;
  createdAt: string;
}

export interface ArtifactPreview {
  artifactId: string;
  mediaType: string;
  sizeBytes: number;
  truncated: boolean;
  text?: string;
  dataUri?: string;
}

export interface ProviderSummary {
  id: string;
  baseUrl: string;
  timeoutSeconds: number;
  /** 只表示密钥是否已配置；密钥值本身永远不跨进程回流。 */
  secretConfigured: boolean;
  selected: boolean;
}

export interface ProviderList {
  providers: ProviderSummary[];
  model: string;
}

export interface ProviderUpsertInput {
  id: string;
  baseUrl: string;
  timeoutSeconds: number;
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
  restartCore(): Promise<DesktopBootstrap>;
  listArtifacts(sessionKey: string, limit?: number): Promise<ArtifactSummary[]>;
  previewArtifact(artifactId: string, maxBytes?: number): Promise<ArtifactPreview>;
  revealArtifact(artifactId: string): Promise<void>;
  pickAttachment(): Promise<string | null>;
  stageAttachment(path: string): Promise<AttachmentRef>;
  listProviders(): Promise<ProviderList>;
  upsertProvider(input: ProviderUpsertInput): Promise<void>;
  removeProvider(id: string): Promise<void>;
  selectProvider(id: string, model: string): Promise<void>;
  setProviderSecret(id: string, value: string): Promise<void>;
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
  coreRestart: "desktop:core:restart",
  artifactsList: "desktop:artifacts:list",
  artifactPreview: "desktop:artifacts:preview",
  artifactReveal: "desktop:artifacts:reveal",
  attachmentPick: "desktop:attachment:pick",
  attachmentStage: "desktop:attachment:stage",
  providersList: "desktop:providers:list",
  providerUpsert: "desktop:providers:upsert",
  providerRemove: "desktop:providers:remove",
  providerSelect: "desktop:providers:select",
  providerSecret: "desktop:providers:secret",
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
    restartCore: () => invoke(DESKTOP_CHANNELS.coreRestart) as Promise<DesktopBootstrap>,
    listArtifacts: (sessionKey, limit = 50) =>
      invoke(DESKTOP_CHANNELS.artifactsList, { sessionKey, limit }) as Promise<ArtifactSummary[]>,
    previewArtifact: (artifactId, maxBytes = 65_536) =>
      invoke(DESKTOP_CHANNELS.artifactPreview, { artifactId, maxBytes }) as Promise<ArtifactPreview>,
    revealArtifact: (artifactId) =>
      invoke(DESKTOP_CHANNELS.artifactReveal, { artifactId }) as Promise<void>,
    pickAttachment: () =>
      invoke(DESKTOP_CHANNELS.attachmentPick) as Promise<string | null>,
    stageAttachment: (path) =>
      invoke(DESKTOP_CHANNELS.attachmentStage, { path }) as Promise<AttachmentRef>,
    listProviders: () => invoke(DESKTOP_CHANNELS.providersList) as Promise<ProviderList>,
    upsertProvider: (input) =>
      invoke(DESKTOP_CHANNELS.providerUpsert, input) as Promise<void>,
    removeProvider: (id) => invoke(DESKTOP_CHANNELS.providerRemove, { id }) as Promise<void>,
    selectProvider: (id, model) =>
      invoke(DESKTOP_CHANNELS.providerSelect, { id, model }) as Promise<void>,
    setProviderSecret: (id, value) =>
      invoke(DESKTOP_CHANNELS.providerSecret, { id, value }) as Promise<void>,
    onFrame: (handler) =>
      subscribe(DESKTOP_CHANNELS.frame, (value) => handler(value as ServerFrame)),
  };
}

declare global {
  interface Window {
    lobster0: DesktopApi;
  }
}
