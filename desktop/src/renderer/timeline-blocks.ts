import type { TimelineItem } from "@lobster0/pi-tui/state";

/** 用户与助手的正式发言，始终独立展开显示。 */
export type MessageItem = Extract<TimelineItem, { kind: "user" | "assistant" }>;

/** 一次回合中的中间过程：思考、工具调用与本地提示。 */
export type ProcessItem = Exclude<TimelineItem, MessageItem>;

export type TimelineBlock =
  | { kind: "message"; id: number; item: MessageItem }
  | { kind: "process"; id: number; items: ProcessItem[] };

function isMessage(item: TimelineItem): item is MessageItem {
  return item.kind === "user" || item.kind === "assistant";
}

/**
 * 把时间线压成「消息」与「过程」两种块。
 *
 * 连续的思考、工具调用和本地提示会合并进同一个「过程」块，这样界面上呈现为
 * 一整段可收起的过程，而不是每条思考、每次工具调用各占一行。遇到用户或助手
 * 发言就结束当前过程块，保证过程始终归属于它所在的那一段对话。
 */
export function groupTimeline(items: readonly TimelineItem[]): TimelineBlock[] {
  const blocks: TimelineBlock[] = [];
  let pending: ProcessItem[] = [];

  const flush = (): void => {
    const [first] = pending;
    if (first) {
      blocks.push({ kind: "process", id: first.id, items: pending });
      pending = [];
    }
  };

  for (const item of items) {
    if (isMessage(item)) {
      flush();
      blocks.push({ kind: "message", id: item.id, item });
    } else {
      pending.push(item);
    }
  }
  flush();
  return blocks;
}

/**
 * 返回值得单独展示的工具摘要，没有就返回 null。
 *
 * Core 当前在 `agent/runner.py` 里把 `summary` 直接设成工具名，若照单全收会在
 * 工具名下方再重复渲染一模一样的一行。这里只在摘要真的携带额外信息时才展示。
 */
export function toolDetail(tool: { name: string; summary: string }): string | null {
  const summary = tool.summary.trim();
  if (!summary || summary === tool.name.trim()) {
    return null;
  }
  return summary;
}
