/** Keyboard-first approval overlay controlled only by Core grant modes. */

import {
  Key,
  matchesKey,
  truncateToWidth,
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

  public constructor(
    private readonly approval: PendingApproval,
    private readonly language: UiLanguage,
    private readonly onDecision: (decision: ApprovalChoice) => void,
  ) {}

  public invalidate(): void {}

  public handleInput(data: string): void {
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
    const border = `─`.repeat(Math.max(1, innerWidth - title.length - 2));
    const lines = [palette.amber(`┌─ ${title} ${border}┐`)];
    const details = `${terminalSafe(this.approval.summary)}\n${terminalSafe(JSON.stringify(this.approval.arguments, null, 2))}`;
    for (const raw of details.split("\n").flatMap((line) => wrapTextWithAnsi(line || " ", innerWidth))) {
      lines.push(`│ ${truncateToWidth(raw, innerWidth, "").padEnd(innerWidth)} │`);
    }
    const choices = [zh ? "[1 拒绝]" : "[1 Deny]"];
    if (this.allowed("once")) choices.push(zh ? "[2 仅一次]" : "[2 Once]");
    if (this.allowed("session")) choices.push(zh ? "[3 本次运行]" : "[3 Session]");
    if (this.allowed("always")) choices.push(zh ? "[4 始终允许]" : "[4 Always]");
    for (const raw of wrapTextWithAnsi(choices.join("  "), innerWidth)) {
      lines.push(`│ ${truncateToWidth(raw, innerWidth, "").padEnd(innerWidth)} │`);
    }
    lines.push(palette.amber(`└${"─".repeat(safeWidth - 2)}┘`));
    return lines.map((line) => truncateToWidth(line, safeWidth, ""));
  }

  private allowed(decision: ApprovalChoice): boolean {
    return decision === "deny" || this.approval.grantModes.includes(decision);
  }
}
