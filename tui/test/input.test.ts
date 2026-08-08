import assert from "node:assert/strict";
import test from "node:test";

import { TuiAltScreen } from "@earendil-works/pi-tui";

import {
  MiniClawTui,
  type BridgePort,
  type ClipboardPort,
} from "../dist/app.js";
import type { BridgeFatalHandler, BridgeEventHandler } from "../dist/bridge-client.js";
import type { JsonValue, ServerFrame } from "../dist/protocol.js";
import { MemoryTerminal } from "./helpers.ts";

class FakeBridge implements BridgePort {
  public readonly turns: { session: string; text: string }[] = [];
  public readonly approvals: { id: number; decision: string }[] = [];
  public cancelled = 0;
  public readonly permissionModes: string[] = [];
  public readonly memoryCommands: Record<string, JsonValue>[] = [];
  public rejectNextTurn = false;
  private eventHandler?: BridgeEventHandler;
  private fatalHandler?: BridgeFatalHandler;

  public async hello(): Promise<Record<string, JsonValue>> {
    return {
      core_version: "0.1.0",
      model: "deepseek-v4-pro",
      workspace: "workspace",
      language: "zh-CN",
      context_budget_tokens: 128_000,
      permission_mode: "safe",
      tools: ["run_command", "read_file"],
    };
  }

  public async startTurn(sessionKey: string, text: string): Promise<void> {
    this.turns.push({ session: sessionKey, text });
    if (this.rejectNextTurn) {
      this.rejectNextTurn = false;
      throw new Error("provider failed with secret body");
    }
  }

  public async cancelTurn(): Promise<void> {
    this.cancelled += 1;
  }

  public async resolveApproval(approvalId: number, decision: string): Promise<void> {
    this.approvals.push({ id: approvalId, decision });
  }

  public async newSession(): Promise<void> {}
  public async memoryCommand(payload: Record<string, JsonValue>): Promise<Record<string, JsonValue>> {
    this.memoryCommands.push(payload);
    return {
      items: [{ unit_id: "mem-language", text: "用户偏好使用中文回复", status: "active" }],
    };
  }
  public async setPermissionMode(mode: "safe" | "smart" | "autopilot" | "yolo") {
    this.permissionModes.push(mode);
    return mode;
  }
  public async shutdown(): Promise<void> {}
  public kill(): void {}

  public onEvent(handler: BridgeEventHandler): () => void {
    this.eventHandler = handler;
    return () => {
      this.eventHandler = undefined;
    };
  }

  public onFatal(handler: BridgeFatalHandler): () => void {
    this.fatalHandler = handler;
    return () => {
      this.fatalHandler = undefined;
    };
  }

  public emit(type: string, payload: Record<string, JsonValue>): void {
    this.eventHandler?.({ v: 1, type, payload } as ServerFrame);
  }
}

class FakeClipboard implements ClipboardPort {
  public copied: string[] = [];

  public copy(text: string): boolean {
    this.copied.push(text);
    return true;
  }
}

async function createApp(): Promise<{
  app: MiniClawTui;
  bridge: FakeBridge;
  terminal: MemoryTerminal;
  clipboard: FakeClipboard;
}> {
  const bridge = new FakeBridge();
  const terminal = new MemoryTerminal(80, 24);
  const clipboard = new FakeClipboard();
  const app = new MiniClawTui({
    tui: new TuiAltScreen(terminal, false, undefined, { mouse: true }),
    bridge,
    clipboard,
    language: "zh-CN",
    sessionKey: "default",
  });
  await app.start();
  return { app, bridge, terminal, clipboard };
}

test("large bracketed paste submits the full original text once", async () => {
  const { app, bridge, terminal } = await createApp();
  const text = `长文本\n${"数".repeat(250_000)}`;

  app.editor.handleInput(`\u001b[200~${text}\u001b[201~`);
  terminal.send("\r");
  await app.whenIdle();

  assert.deepEqual(bridge.turns, [{ session: "default", text }]);
  assert.equal(app.editor.getExpandedText(), "");
  app.stop();
});

test("memory slash command uses the typed bridge surface and renders results", async () => {
  const { app, bridge, terminal } = await createApp();
  app.editor.setText("/memory search 中文回复");

  terminal.send("\r");
  await app.whenIdle();

  assert.deepEqual(bridge.memoryCommands, [
    { action: "search", query: "中文回复", limit: 10 },
  ]);
  assert.match(app.renderDocument(80).join("\n"), /mem-language.*中文回复/);
  app.stop();
});

test("memory review decisions carry the exact preview hash", async () => {
  const { app, bridge, terminal } = await createApp();
  const previewHash = "a".repeat(64);
  app.editor.setText(`/memory approve 7 ${previewHash}`);

  terminal.send("\r");
  await app.whenIdle();

  assert.deepEqual(bridge.memoryCommands, [
    { action: "approve", review_id: 7, preview_hash: previewHash },
  ]);
  app.stop();
});

test("memory forget only creates a preview request", async () => {
  const { app, bridge, terminal } = await createApp();
  app.editor.setText("/memory forget mem-language");

  terminal.send("\r");
  await app.whenIdle();

  assert.deepEqual(bridge.memoryCommands, [
    { action: "forget", unit_id: "mem-language" },
  ]);
  app.stop();
});

test("memory rebuild uses a no-argument owner-bound command", async () => {
  const { app, bridge, terminal } = await createApp();
  app.editor.setText("/memory rebuild");

  terminal.send("\r");
  await app.whenIdle();

  assert.deepEqual(bridge.memoryCommands, [{ action: "rebuild" }]);
  app.stop();
});

test("shift enter inserts a newline without starting a turn", async () => {
  const { app, bridge, terminal } = await createApp();
  app.editor.setText("第一行");

  terminal.send("\u001b[13;2u");
  app.editor.handleInput("第二行");

  assert.equal(app.editor.getExpandedText(), "第一行\n第二行");
  assert.equal(bridge.turns.length, 0);
  app.stop();
});

test("failed submit restores the exact long draft and shows a safe error", async () => {
  const { app, bridge, terminal } = await createApp();
  const text = `保留草稿\n${"字".repeat(40_000)}`;
  bridge.rejectNextTurn = true;
  app.editor.setText(text);

  terminal.send("\r");
  await app.whenIdle();

  assert.equal(app.editor.getExpandedText(), text);
  const rendered = app.renderDocument(80).join("\n");
  assert.match(rendered, /本轮失败/);
  assert.doesNotMatch(rendered, /secret body|provider failed/);
  app.stop();
});

test("provider failure event restores the submitted draft without exposing diagnostics", async () => {
  const { app, bridge, terminal } = await createApp();
  const text = `模型调用中途失败也要恢复\n${"草".repeat(12_000)}`;
  app.editor.setText(text);

  terminal.send("\r");
  await app.whenIdle();
  assert.equal(app.editor.getExpandedText(), "");

  bridge.emit("event.turn_failed", {
    turn_id: 7,
    error_code: "ProviderProtocolError",
    diagnostic: "secret provider response body",
  });

  assert.equal(app.editor.getExpandedText(), text);
  const rendered = app.renderDocument(80).join("\n");
  assert.match(rendered, /ProviderProtocolError/);
  assert.doesNotMatch(rendered, /secret provider response body/);
  app.stop();
});

test("unexpected Core operation failure unlocks input with a safe summary", async () => {
  const { app, bridge, terminal } = await createApp();
  const text = "未知 Core 异常也不能把界面锁死";
  app.editor.setText(text);
  terminal.send("\r");
  await app.whenIdle();

  bridge.emit("event.bridge_error", {
    code: "core_operation_failed",
    message: "secret traceback and provider body",
    retryable: false,
  });

  assert.equal(app.editor.getExpandedText(), text);
  assert.equal(app.editor.disableSubmit, false);
  const rendered = app.renderDocument(80).join("\n");
  assert.match(rendered, /core_operation_failed/);
  assert.doesNotMatch(rendered, /secret traceback|provider body/);
  app.stop();
});

test("one hundred deltas keep one transcript component and manual scroll position", async () => {
  const { app, bridge } = await createApp();
  const timelineIdentity = app.timeline;
  for (let index = 0; index < 60; index += 1) {
    app.appendLocal(`history-${index}\nsecond line`);
  }
  app.tui.renderNow(true);
  app.scrollView.scrollToStart();
  const before = app.scrollView.scrollTop;

  for (let index = 0; index < 100; index += 1) {
    bridge.emit("event.model_text_delta", { turn_id: 9, text: String(index % 10) });
  }
  app.tui.renderNow(true);

  assert.equal(app.timeline, timelineIdentity);
  assert.equal(app.state.timeline.filter((item) => item.kind === "assistant").length, 1);
  assert.equal(app.scrollView.scrollTop, before);
  app.stop();
});

test("mouse drag copies stable transcript text through OSC52", async () => {
  const { app, terminal } = await createApp();
  app.appendLocal("可稳定选择复制的活动文本");
  app.tui.renderNow(true);
  const before = terminal.writes.length;

  // SGR mouse coordinates are one-based. Select six cells from the first
  // transcript row, then release. TuiAltScreen owns and copies the selection.
  terminal.send("\u001b[<0;2;2M");
  terminal.send("\u001b[<32;8;2M");
  terminal.send("\u001b[<0;8;2m");

  const writes = terminal.writes.slice(before).join("");
  assert.match(writes, /\u001b\]52;c;[A-Za-z0-9+/=]+\u0007/);
  app.stop();
});

test("follow mode stays at the bottom while streamed output grows", async () => {
  const { app, bridge } = await createApp();
  for (let index = 0; index < 80; index += 1) {
    app.appendLocal(`activity-${index}`);
  }
  app.tui.renderNow(true);
  app.scrollView.scrollToEnd();
  const before = app.scrollView.scrollTop;

  for (let index = 0; index < 40; index += 1) {
    bridge.emit("event.model_text_delta", { turn_id: 10, text: `流-${index}\n` });
  }
  app.tui.renderNow(true);

  assert.equal(app.scrollView.isFollowingEnd, true);
  assert.equal(app.scrollView.scrollTop > before, true);
  app.stop();
});

test("approval overlay sends the selected scoped decision to Core", async () => {
  const { app, bridge, terminal } = await createApp();
  bridge.emit("event.approval_required", {
    turn_id: 11,
    approval_id: 9,
    call_id: "call-9",
    tool_name: "run_command",
    summary: "run lark-cli",
    arguments: { program: "/usr/local/bin/lark-cli", args: ["doc", "list"] },
    grant_modes: ["once", "session", "always"],
  });

  assert.notEqual(app.approvalDialog, null);
  terminal.send("3");
  await app.whenIdle();

  assert.deepEqual(bridge.approvals, [{ id: 9, decision: "session" }]);
  assert.equal(app.approvalDialog, null);
  app.stop();
});

test("copy, language and trace commands stay local", async () => {
  const { app, bridge, terminal, clipboard } = await createApp();
  bridge.emit("event.model_usage", {
    turn_id: 5,
    context_tokens: 4096,
    input_tokens: 1024,
    output_tokens: 128,
    tool_calls: 2,
    iteration: 3,
    provider_request_id: "req-debug-42",
  });
  bridge.emit("event.turn_finished", {
    turn_id: 5,
    status: "completed",
    content: "完整\n回复",
  });

  app.editor.setText("/copy");
  terminal.send("\r");
  await app.whenIdle();
  app.editor.setText("/lang en");
  terminal.send("\r");
  await app.whenIdle();
  app.editor.setText("/status");
  terminal.send("\r");
  await app.whenIdle();

  assert.deepEqual(clipboard.copied, ["完整\n回复"]);
  assert.equal(app.language, "en");
  assert.equal(bridge.turns.length, 0);
  assert.match(app.renderDocument(80).join("\n"), /req-debug-42/);
  app.stop();
});

test("permissions command shows and changes the shared Core mode without a model turn", async () => {
  const { app, bridge, terminal } = await createApp();

  app.editor.setText("/permissions");
  terminal.send("\r");
  await app.whenIdle();
  assert.match(app.renderDocument(80).join("\n"), /当前权限模式：safe/);

  app.editor.setText("/permissions autopilot");
  terminal.send("\r");
  await app.whenIdle();
  const output = app.renderDocument(100).join("\n");
  assert.deepEqual(bridge.permissionModes, ["autopilot"]);
  assert.match(output, /AUTOPILOT/);
  assert.match(output, /权限模式已切换为：autopilot/);
  assert.equal(bridge.turns.length, 0);
  app.stop();
});
