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
 * 描述一个过程块里有什么，用作折叠标题。
 *
 * 原先显示的是 `items.length`「N 步」，但那个 N 把两种完全不同的东西当成同一
 * 单位：一整轮的思考只产生**一个** reasoning 条目（`event.model_reasoning` 来
 * 一次就追加一条，正文再长也是一条），于是一屏思考和一次 `read_file` 都显示
 * 「1 步」。
 *
 * 「步」承诺的是可数的动作，而思考不是动作。因此**只数工具调用**——调了三次
 * 就是三次，数字有意义；思考写成「思考」，想知道多长展开就看到了。
 *
 * 不去解析思考正文推断步骤数：模型的思考没有稳定结构，按换行或编号去数得到的
 * 是排版的数字而不是过程的数字，看起来更精确，实则误导更深。
 */
export function processSummary(items: readonly ProcessItem[]): string {
  const tools = items.filter((item) => item.kind === "tool").length;
  const parts: string[] = [];
  if (items.some((item) => item.kind === "reasoning")) {
    parts.push("思考");
  }
  if (tools > 0) {
    parts.push(`${tools} 次工具调用`);
  }
  // 只有本地提示时也要说点什么：空标题无法区分「没有内容」与「坏了」。
  // 本地提示本身不计入——它是界面对用户说的话，不是 Agent 做的事。
  return parts.length > 0 ? parts.join(" · ") : "运行记录";
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
