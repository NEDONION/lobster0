import assert from "node:assert/strict";
import test from "node:test";

import { visibleWidth } from "@earendil-works/pi-tui";

import { ApprovalDialog } from "../dist/components/approval.js";
import type { PendingApproval } from "../dist/state.js";

function approval(grantModes: ("once" | "session" | "always")[]): PendingApproval {
  return {
    approvalId: 7,
    turnId: 21,
    callId: "call-7",
    toolName: "run_command",
    summary: "run lark-cli",
    arguments: {
      program: "/usr/local/bin/lark-cli",
      args: ["doc", "list", "--created-this-week"],
    },
    grantModes,
  };
}

test("approval dialog renders only Core-authorized grant modes", () => {
  const dialog = new ApprovalDialog(approval(["once", "session"]), "zh-CN", () => {});
  const lines = dialog.render(70);
  const output = lines.join("\n");

  assert.match(output, /拒绝/);
  assert.match(output, /仅一次/);
  assert.match(output, /本次运行/);
  assert.doesNotMatch(output, /始终允许/);
  assert.equal(lines.every((line) => visibleWidth(line) <= 70), true);
});

test("approval keyboard decisions map exactly to deny once session always", () => {
  const decisions: string[] = [];
  const dialog = new ApprovalDialog(
    approval(["once", "session", "always"]),
    "zh-CN",
    (decision) => decisions.push(decision),
  );

  dialog.handleInput("1");
  dialog.handleInput("2");
  dialog.handleInput("3");
  dialog.handleInput("4");
  dialog.handleInput("\u001b");

  assert.deepEqual(decisions, ["deny", "once", "session", "always", "deny"]);
});
