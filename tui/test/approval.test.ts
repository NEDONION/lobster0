import assert from "node:assert/strict";
import test from "node:test";

import { visibleWidth } from "@earendil-works/pi-tui";

import { ApprovalDialog } from "../dist/components/approval.js";
import type { PendingApproval } from "../dist/state.js";

function approval(
  grantModes: ("once" | "session" | "always")[],
  toolName = "run_command",
): PendingApproval {
  return {
    approvalId: 7,
    turnId: 21,
    callId: "call-7",
    toolName,
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

test("run command approval explains current-user OS access in both languages", () => {
  const zh = new ApprovalDialog(approval(["once"]), "zh-CN", () => {}).render(120).join("\n");
  const en = new ApprovalDialog(approval(["once"]), "en-US", () => {}).render(120).join("\n");

  assert.match(zh, /当前用户身份运行/);
  assert.match(zh, /当前用户可访问的文件/);
  assert.match(en, /current user/);
  assert.match(en, /files accessible to that user/);
});

test("file approval does not show the command OS-access warning", () => {
  const output = new ApprovalDialog(
    approval(["once"], "write_file"),
    "zh-CN",
    () => {},
  ).render(48).join("\n");

  assert.doesNotMatch(output, /当前用户身份运行/);
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

test("long approval stays within 18 rows with sticky warning and decisions", () => {
  const value = approval(["once", "session", "always"]);
  value.arguments = {
    program: "/usr/local/bin/lark-cli",
    args: Array.from({ length: 80 }, (_, index) => `document-${index}`),
  };
  const dialog = new ApprovalDialog(value, "zh-CN", () => {}, 18);

  const first = dialog.render(80);
  assert.equal(first.length <= 18, true);
  assert.equal(first.every((line) => visibleWidth(line) <= 80), true);
  assert.match(first.join("\n"), /审批 #7/);
  assert.match(first.join("\n"), /当前用户身份运行/);
  assert.match(first.join("\n"), /拒绝/);
  assert.match(first.join("\n"), /始终允许/);
  assert.doesNotMatch(first.join("\n"), /document-79/);

  dialog.handleInput("\u001b[6~");
  const next = dialog.render(80).join("\n");
  assert.doesNotMatch(next, /document-0\"/);
  assert.match(next, /详情/);

  dialog.handleInput("\u001b[F");
  const end = dialog.render(80).join("\n");
  assert.match(end, /document-79/);
  assert.match(end, /始终允许/);
});
