import type { ServerFrame } from "@lobster0/pi-tui/protocol";
import {
  appendUser,
  createInitialState,
  reduceFrame,
  type AppState,
  type Telemetry,
} from "@lobster0/pi-tui/state";

import type { AttachmentRef, SessionHistory, SessionMessage } from "../common/api";

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
  /**
   * 每个已完成回合的 telemetry 快照，按 turnId 索引。
   *
   * `AppState.telemetry` 只保留最近一次运行的数字，会被下一回合覆盖；
   * 而界面要在每条回复下方显示"这一条"的耗时与用量，因此在回合结束时
   * 把当时的读数固定下来。失败或取消的回合不记录——那些数字不完整，
   * 展示出来会误导。
   */
  turnTelemetry: Readonly<Record<number, Telemetry>>;
  /**
   * 下一段流式文本是否需要另起一段。
   *
   * 一个回合里模型可能产出多轮文字（每轮之间夹着工具调用），Core 把它们
   * 作为独立的 delta 发出、且不带换行，pi-tui 又全部追加进同一条消息，
   * 结果就是几百字糊成一整段。工具调用意味着上一段话已经说完，因此在
   * 它之后的第一段新文本前补一个空行。
   */
  pendingParagraphBreak: boolean;
  /**
   * 每条用户消息带的附件，按时间线条目 id 索引。
   *
   * 为什么是旁挂的表而不是 `UserItem` 上的字段：`UserItem` 定义在共享库
   * `@lobster0/pi-tui` 里，终端 TUI 也在用它。终端渲染不了缩略图，为它加一个
   * 永远用不上的字段还要重建 dist，代价不划算。这张表只在追加条目时写入、
   * 此后不再变动，不存在与时间线不同步的窗口。
   *
   * 只存摘要，缩略图按需通过 `previewArtifact` 取——见
   * docs/superpowers/specs/2026-08-12-desktop-attachment-in-message-design.md。
   */
  attachmentsByItemId: Readonly<Record<number, AttachmentRef[]>>;
}

export function createDesktopTaskState(sessionKey: string): DesktopTaskState {
  return {
    sessionKey,
    status: "idle",
    run: createInitialState(),
    error: null,
    turnTelemetry: {},
    pendingParagraphBreak: false,
    attachmentsByItemId: {},
  };
}

export function appendDesktopUser(
  state: DesktopTaskState,
  content: string,
  attachments: readonly AttachmentRef[] = [],
): DesktopTaskState {
  // 条目 id 取自追加**之前**的 nextItemId——appendUser 就是用它建条目的。
  const itemId = state.run.nextItemId;
  const next = { ...state, run: appendUser(state.run, content), error: null };
  if (attachments.length === 0) {
    // 不写空数组：空数组与「没有」在渲染层是两种分支，会让气泡多出一个空容器。
    return next;
  }
  return {
    ...next,
    attachmentsByItemId: { ...next.attachmentsByItemId, [itemId]: [...attachments] },
  };
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

/** 给最近一个尚无预览的工具条目补上结果正文。 */
function finishLatestTool(
  state: DesktopTaskState,
  message: SessionMessage,
): DesktopTaskState {
  const pending = [...state.run.timeline]
    .reverse()
    .find((item) => item.kind === "tool" && !item.preview);
  if (pending === undefined || pending.kind !== "tool") {
    return state;
  }
  return {
    ...state,
    run: reduceFrame(state.run, {
      v: 1,
      type: "event.tool_finished",
      payload: {
        turn_id: message.turnId ?? 0,
        call_id: pending.callId,
        status: "succeeded",
        preview: message.content,
        duration_ms: null,
      },
    }),
  };
}

/** 把一条历史消息还原成时间线条目，含思考与工具调用。 */
function hydrateMessage(state: DesktopTaskState, message: SessionMessage): DesktopTaskState {
  const turnId = message.turnId ?? 0;
  if (message.role === "user") {
    return appendDesktopUser(state, message.content, message.attachments ?? []);
  }
  if (message.role === "tool") {
    // 工具条目由发起它的那条 Assistant 生成（那里才有工具名与顺序）；
    // 结果消息只补上正文预览。
    return finishLatestTool(state, message);
  }
  let next = state;
  for (const name of message.toolCalls ?? []) {
    // 历史里没有 requested/started/finished 三段事件，只有最终态，
    // 所以直接构造一个工具条目，再由随后的结果消息补预览。
    next = {
      ...next,
      run: reduceFrame(next.run, {
        v: 1,
        type: "event.tool_requested",
        payload: {
          turn_id: turnId,
          call_id: `history-${next.run.nextItemId}`,
          tool_name: name,
          summary: "",
          arguments: {},
        },
      }),
    };
  }
  if (message.reasoning) {
    next = {
      ...next,
      run: reduceFrame(next.run, {
        v: 1,
        type: "event.model_reasoning",
        payload: { turn_id: turnId, text: message.reasoning },
      }),
    };
  }
  // 正文为空时不再丢掉整条：那一轮往往正是"只调了工具"的关键一步。
  if (message.content) {
    next = {
      ...next,
      run: reduceFrame(next.run, {
        v: 1,
        type: "event.turn_finished",
        payload: { turn_id: turnId, content: message.content },
      }),
    };
  }
  return next;
}

export function hydrateSession(history: SessionHistory): DesktopTaskState {
  let state = createDesktopTaskState(history.sessionKey);
  for (const message of history.messages) {
    state = hydrateMessage(state, message);
  }
  const latest = history.turns.at(-1);
  if (!latest) {
    return state;
  }
  if (latest.status === "failed" && latest.errorCode === "runtime_interrupted") {
    return { ...state, status: "interrupted", error: "上次运行意外中断" };
  }
  if (latest.status === "failed") {
    return {
      ...state,
      status: "failed",
      error: `本轮失败：${stableCode(latest.errorCode, "agent")}`,
    };
  }
  if (latest.status === "queued" || latest.status === "running") {
    return { ...state, status: "running" };
  }
  if (latest.status === "waiting_approval") {
    return { ...state, status: "waiting_approval" };
  }
  if (latest.status === "cancelled") {
    return { ...state, status: "cancelled" };
  }
  return { ...state, status: latest.status === "completed" ? "completed" : "idle" };
}

export function reduceDesktopFrame(
  state: DesktopTaskState,
  frame: ServerFrame,
): DesktopTaskState {
  // 折叠状态不再挂在逐条 item 上：Desktop 把连续的思考与工具聚合成「过程」块，
  // 由渲染层维护块级折叠，因此这里不需要再干预 pi-tui 的 expanded 默认值。
  const reduced = reduceFrame(state.run, withParagraphBreak(state, frame));
  const pendingParagraphBreak = nextParagraphBreak(state, frame);
  if (frame.type === "event.turn_started") {
    return { ...state, status: "running", run: reduced, error: null, pendingParagraphBreak };
  }
  if (frame.type === "event.approval_required") {
    return { ...state, status: "waiting_approval", run: reduced, error: null, pendingParagraphBreak };
  }
  if (frame.type === "event.turn_finished") {
    const turnId = frame.payload.turn_id;
    return {
      ...state,
      status: "completed",
      run: reduced,
      error: null,
      pendingParagraphBreak: false,
      turnTelemetry:
        typeof turnId === "number"
          ? { ...state.turnTelemetry, [turnId]: reduced.telemetry }
          : state.turnTelemetry,
    };
  }
  if (frame.type === "event.turn_failed") {
    return {
      ...state,
      status: "failed",
      run: terminal(reduced),
      error: `本轮失败：${stableCode(frame.payload.error_code, "agent")}`,
      pendingParagraphBreak: false,
    };
  }
  if (frame.type === "event.turn_cancelled") {
    return { ...state, status: "cancelled", run: terminal(reduced), error: null, pendingParagraphBreak: false };
  }
  if (frame.type === "event.bridge_error") {
    return {
      ...state,
      status: "failed",
      run: terminal(reduced),
      error: `Core 操作失败：${stableCode(frame.payload.code, "core_operation_failed")}`,
      pendingParagraphBreak: false,
    };
  }
  return { ...state, run: reduced, pendingParagraphBreak };
}

/** 工具调用把一段话切断了，下一段文本应当另起一段。 */
function nextParagraphBreak(state: DesktopTaskState, frame: ServerFrame): boolean {
  if (frame.type === "event.tool_requested") {
    return true;
  }
  if (frame.type === "event.model_text_delta") {
    return false;
  }
  return state.pendingParagraphBreak;
}

/** 在需要断段时，给这一片流式文本前补一个空行；其余帧原样透传。 */
function withParagraphBreak(state: DesktopTaskState, frame: ServerFrame): ServerFrame {
  if (!state.pendingParagraphBreak || frame.type !== "event.model_text_delta") {
    return frame;
  }
  const text = frame.payload.text;
  if (typeof text !== "string" || text.length === 0) {
    return frame;
  }
  return { ...frame, payload: { ...frame.payload, text: `\n\n${text}` } };
}

function terminal(state: AppState): AppState {
  return { ...state, busy: false, activeTurnId: null, pendingApproval: null };
}

function stableCode(value: unknown, fallback: string): string {
  return typeof value === "string" && /^[a-z0-9_.-]{1,64}$/.test(value) ? value : fallback;
}
