/** Keyboard-first approval overlay controlled only by Core grant modes. */

import {
  Key,
  matchesKey,
  truncateToWidth,
  visibleWidth,
  wrapTextWithAnsi,
  type Component,
  type Focusable,
} from "@earendil-works/pi-tui";

import type { PendingApproval } from "../state.js";
import { palette, terminalSafe } from "../theme.js";
import type { UiLanguage } from "./conversation.js";

export type ApprovalChoice = "deny" | "once" | "session" | "always";

export class ApprovalDialog implements Component, Focusable {
  public focused = false;
  private scrollOffset = 0;
  private pageSize = 1;
  private detailCount = 0;

  public constructor(
    private readonly approval: PendingApproval,
    private readonly language: UiLanguage,
    private readonly onDecision: (decision: ApprovalChoice) => void,
    private readonly maxRows = 18,
  ) {}

  public invalidate(): void {}

  public handleInput(data: string): void {
    const maxOffset = Math.max(0, this.detailCount - this.pageSize);
    if (matchesKey(data, Key.up)) {
      this.scrollOffset = Math.max(0, this.scrollOffset - 1);
      return;
    }
    if (matchesKey(data, Key.down)) {
      this.scrollOffset = Math.min(maxOffset, this.scrollOffset + 1);
      return;
    }
    if (matchesKey(data, Key.pageUp)) {
      this.scrollOffset = Math.max(0, this.scrollOffset - this.pageSize);
      return;
    }
    if (matchesKey(data, Key.pageDown)) {
      this.scrollOffset = Math.min(maxOffset, this.scrollOffset + this.pageSize);
      return;
    }
    if (matchesKey(data, Key.home)) {
      this.scrollOffset = 0;
      return;
    }
    if (matchesKey(data, Key.end)) {
      this.scrollOffset = maxOffset;
      return;
    }
    if (matchesKey(data, Key.escape) || data === "1") {
      this.onDecision("deny");
      return;
    }
    const mapping: Record<string, ApprovalChoice> = { "2": "once", "3": "session", "4": "always" };
    const decision = mapping[data];
    if (decision && this.allowed(decision)) {
      this.onDecision(decision);
    }
  }

  public render(width: number): string[] {
    const safeWidth = Math.max(20, width);
    const innerWidth = Math.max(1, safeWidth - 4);
    const zh = this.language === "zh-CN";
    const title = `${zh ? "审批" : "Approval"} #${this.approval.approvalId} · ${terminalSafe(this.approval.toolName)}`;
    const border = `─`.repeat(Math.max(1, innerWidth - visibleWidth(title) - 2));
    const header = palette.amber(`┌─ ${title} ${border}┐`);
    const details = `${terminalSafe(this.approval.summary)}\n${terminalSafe(JSON.stringify(this.approval.arguments, null, 2))}`;
    const detailLines = details
      .split("\n")
      .flatMap((line) => wrapTextWithAnsi(line || " ", innerWidth));
    this.detailCount = detailLines.length;
    const footer: string[] = [];
    if (this.approval.toolName === "run_command") {
      const warning = zh
        ? "注意：该程序将以当前用户身份运行，并可能读取当前用户可访问的文件。"
        : "Note: This program runs as the current user and may read files accessible to that user.";
      for (const raw of wrapTextWithAnsi(warning, innerWidth)) {
        footer.push(boxLine(palette.muted(raw), innerWidth));
      }
    }
    const choices = [zh ? "[1 拒绝]" : "[1 Deny]"];
    if (this.allowed("once")) choices.push(zh ? "[2 仅一次]" : "[2 Once]");
    if (this.allowed("session")) choices.push(zh ? "[3 本次运行]" : "[3 Session]");
    if (this.allowed("always")) choices.push(zh ? "[4 始终允许]" : "[4 Always]");
    for (const raw of wrapTextWithAnsi(choices.join("  "), innerWidth)) {
      footer.push(boxLine(raw, innerWidth));
    }

    const rowLimit = Math.max(8, this.maxRows);
    const needsScroll = detailLines.length + footer.length + 2 > rowLimit;
    const fixedRows = 2 + footer.length + (needsScroll ? 1 : 0);
    this.pageSize = Math.max(1, rowLimit - fixedRows);
    const maxOffset = Math.max(0, detailLines.length - this.pageSize);
    this.scrollOffset = Math.min(this.scrollOffset, maxOffset);
    const visibleDetails = detailLines.slice(
      this.scrollOffset,
      this.scrollOffset + this.pageSize,
    );
    const lines = [header];
    if (needsScroll) {
      const start = detailLines.length === 0 ? 0 : this.scrollOffset + 1;
      const end = Math.min(detailLines.length, this.scrollOffset + this.pageSize);
      const scrollLabel = zh
        ? `详情 ${start}-${end}/${detailLines.length} · ↑↓ / PgUp PgDn`
        : `Details ${start}-${end}/${detailLines.length} · ↑↓ / PgUp PgDn`;
      lines.push(boxLine(palette.muted(scrollLabel), innerWidth));
    }
    lines.push(...visibleDetails.map((raw) => boxLine(raw, innerWidth)));
    lines.push(...footer);
    lines.push(palette.amber(`└${"─".repeat(safeWidth - 2)}┘`));
    return lines.map((line) => truncateToWidth(line, safeWidth, ""));
  }

  private allowed(decision: ApprovalChoice): boolean {
    return decision === "deny" || this.approval.grantModes.includes(decision);
  }
}

function boxLine(content: string, innerWidth: number): string {
  const clipped = truncateToWidth(content, innerWidth, "");
  const padding = " ".repeat(Math.max(0, innerWidth - visibleWidth(clipped)));
  return `│ ${clipped}${padding} │`;
}
