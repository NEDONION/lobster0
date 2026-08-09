/** Pure event projection used by both the pi-tui shell and regression tests. */

import type { JsonValue, ServerFrame } from "./protocol.js";

export type ApprovalMode = "once" | "session" | "always";

export interface UserItem {
  kind: "user";
  id: number;
  content: string;
}

export interface AssistantItem {
  kind: "assistant";
  id: number;
  turnId: number;
  content: string;
  streaming: boolean;
}

export interface ReasoningItem {
  kind: "reasoning";
  id: number;
  turnId: number;
  content: string;
  expanded: boolean;
}

export interface ToolItem {
  kind: "tool";
  id: number;
  turnId: number;
  callId: string;
  name: string;
  summary: string;
  arguments: Record<string, JsonValue>;
  status: string;
  lifecycle: ("requested" | "started" | "finished")[];
  preview: string;
  durationMs: number | null;
  expanded: boolean;
}

export interface LocalItem {
  kind: "local";
  id: number;
  content: string;
  tone: "info" | "error";
}

export type TimelineItem = UserItem | AssistantItem | ReasoningItem | ToolItem | LocalItem;

export interface Telemetry {
  contextTokens: number | null;
  inputTokens: number | null;
  outputTokens: number | null;
  toolCalls: number;
  iterations: number;
  durationMs: number | null;
  providerRequestId: string | null;
}

export interface PendingApproval {
  approvalId: number;
  turnId: number;
  callId: string;
  toolName: string;
  summary: string;
  arguments: Record<string, JsonValue>;
  grantModes: ApprovalMode[];
}

export interface AppState {
  timeline: TimelineItem[];
  telemetry: Telemetry;
  busy: boolean;
  activeTurnId: number | null;
  pendingApproval: PendingApproval | null;
  nextItemId: number;
  lastAssistantText: string;
}

export function createInitialState(): AppState {
  return {
    timeline: [],
    telemetry: {
      contextTokens: null,
      inputTokens: null,
      outputTokens: null,
      toolCalls: 0,
      iterations: 0,
      durationMs: null,
      providerRequestId: null,
    },
    busy: false,
    activeTurnId: null,
    pendingApproval: null,
    nextItemId: 1,
    lastAssistantText: "",
  };
}

export function appendUser(state: AppState, content: string): AppState {
  return appendItem(state, { kind: "user", id: state.nextItemId, content });
}

export function appendLocal(
  state: AppState,
  content: string,
  tone: "info" | "error" = "info",
): AppState {
  return appendItem(state, { kind: "local", id: state.nextItemId, content, tone });
}

export function reduceFrame(state: AppState, frame: ServerFrame): AppState {
  const payload = frame.payload;
  const turnId = integer(payload.turn_id);
  switch (frame.type) {
    case "event.turn_started":
      return {
        ...state,
        busy: true,
        activeTurnId: turnId,
        telemetry: { ...createInitialState().telemetry },
      };
    case "event.model_text_delta":
      return updateAssistant(state, turnId, string(payload.text), true);
    case "event.model_reasoning":
      return appendItem(state, {
        kind: "reasoning",
        id: state.nextItemId,
        turnId,
        content: string(payload.text),
        expanded: true,
      });
    case "event.model_usage":
      return {
        ...state,
        telemetry: {
          ...state.telemetry,
          contextTokens: nullableInteger(payload.context_tokens),
          inputTokens: nullableInteger(payload.input_tokens),
          outputTokens: nullableInteger(payload.output_tokens),
          toolCalls: integer(payload.tool_calls),
          iterations: integer(payload.iteration),
          providerRequestId: nullableString(payload.provider_request_id),
        },
      };
    case "event.tool_requested":
      return appendItem(state, {
        kind: "tool",
        id: state.nextItemId,
        turnId,
        callId: string(payload.call_id),
        name: string(payload.tool_name),
        summary: string(payload.summary),
        arguments: displayArguments(string(payload.tool_name), record(payload.arguments)),
        status: "requested",
        lifecycle: ["requested"],
        preview: "",
        durationMs: null,
        expanded: false,
      });
    case "event.tool_started":
      return updateTool(state, string(payload.call_id), (tool) => ({
        ...tool,
        status: "running",
        lifecycle: addLifecycle(tool.lifecycle, "started"),
      }));
    case "event.tool_finished":
      return updateTool(state, string(payload.call_id), (tool) => ({
        ...tool,
        status: string(payload.status),
        lifecycle: addLifecycle(tool.lifecycle, "finished"),
        preview: nullableString(payload.preview) ?? "",
        durationMs: nullableInteger(payload.duration_ms),
      }));
    case "event.approval_required":
      return {
        ...state,
        busy: false,
        pendingApproval: {
          approvalId: integer(payload.approval_id),
          turnId,
          callId: string(payload.call_id),
          toolName: string(payload.tool_name),
          summary: string(payload.summary),
          arguments: displayArguments(string(payload.tool_name), record(payload.arguments)),
          grantModes: approvalModes(payload.grant_modes),
        },
      };
    case "event.turn_finished": {
      const content = string(payload.content);
      const updated = updateAssistant(state, turnId, content, false);
      return {
        ...updated,
        busy: false,
        activeTurnId: null,
        pendingApproval: null,
        lastAssistantText: content,
        telemetry: {
          ...updated.telemetry,
          durationMs: nullableInteger(payload.duration_ms),
        },
      };
    }
    case "event.turn_failed":
    case "event.turn_cancelled":
      return { ...state, busy: false, activeTurnId: null };
    default:
      return state;
  }
}

export function setAllExpanded(state: AppState, expanded: boolean): AppState {
  return {
    ...state,
    timeline: state.timeline.map((item) =>
      item.kind === "reasoning" || item.kind === "tool" ? { ...item, expanded } : item,
    ),
  };
}

export function toggleItem(state: AppState, id: number): AppState {
  return {
    ...state,
    timeline: state.timeline.map((item) =>
      item.id === id && (item.kind === "reasoning" || item.kind === "tool")
        ? { ...item, expanded: !item.expanded }
        : item,
    ),
  };
}

function updateAssistant(
  state: AppState,
  turnId: number,
  text: string,
  append: boolean,
): AppState {
  const index = state.timeline.findIndex(
    (item) => item.kind === "assistant" && item.turnId === turnId,
  );
  if (index === -1) {
    return appendItem(state, {
      kind: "assistant",
      id: state.nextItemId,
      turnId,
      content: text,
      streaming: append,
    });
  }
  const timeline = [...state.timeline];
  const current = timeline[index] as AssistantItem;
  timeline[index] = {
    ...current,
    content: append ? current.content + text : text,
    streaming: append,
  };
  return { ...state, timeline };
}

function updateTool(
  state: AppState,
  callId: string,
  update: (tool: ToolItem) => ToolItem,
): AppState {
  return {
    ...state,
    timeline: state.timeline.map((item) =>
      item.kind === "tool" && item.callId === callId ? update(item) : item,
    ),
  };
}

function appendItem(state: AppState, item: TimelineItem): AppState {
  return {
    ...state,
    timeline: [...state.timeline, item],
    nextItemId: state.nextItemId + 1,
  };
}

function addLifecycle<T extends string>(values: T[], value: T): T[] {
  return values.includes(value) ? values : [...values, value];
}

function string(value: JsonValue | undefined): string {
  return typeof value === "string" ? value : "";
}

function nullableString(value: JsonValue | undefined): string | null {
  return typeof value === "string" ? value : null;
}

function integer(value: JsonValue | undefined): number {
  return typeof value === "number" && Number.isInteger(value) ? value : 0;
}

function nullableInteger(value: JsonValue | undefined): number | null {
  return typeof value === "number" && Number.isInteger(value) ? value : null;
}

function record(value: JsonValue | undefined): Record<string, JsonValue> {
  return typeof value === "object" && value !== null && !Array.isArray(value) ? value : {};
}

function displayArguments(
  toolName: string,
  arguments_: Record<string, JsonValue>,
): Record<string, JsonValue> {
  if (!toolName.startsWith("browser_")) return arguments_;
  const fields: Record<string, string[]> = {
    browser_open: ["origin"],
    browser_snapshot: ["cursor"],
    browser_click: ["origin", "role"],
    browser_type: ["origin", "role", "input_kind"],
    browser_press: ["origin", "role", "key"],
    browser_scroll: ["delta_y"],
    browser_screenshot: ["full_page"],
  };
  const visible = Object.fromEntries(
    (fields[toolName] ?? [])
      .filter((name) => Object.hasOwn(arguments_, name))
      .map((name) => [name, arguments_[name] as JsonValue]),
  );
  if (toolName === "browser_type") visible.text = "<redacted>";
  return visible;
}

function approvalModes(value: JsonValue | undefined): ApprovalMode[] {
  if (!Array.isArray(value)) {
    return [];
  }
  return value.filter(
    (item): item is ApprovalMode => item === "once" || item === "session" || item === "always",
  );
}
