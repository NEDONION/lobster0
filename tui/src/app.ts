/** MiniClaw's default pi-tui application shell. */

import { spawnSync } from "node:child_process";

import {
  Editor,
  Key,
  ScrollView,
  VStack,
  isViewportTUI,
  matchesKey,
  type OverlayHandle,
  type TUI,
} from "@earendil-works/pi-tui";

import {
  BridgeRequestError,
  type BridgeEventHandler,
  type BridgeFatalHandler,
} from "./bridge-client.js";
import { ApprovalDialog, type ApprovalChoice } from "./components/approval.js";
import {
  HeaderLine,
  TelemetryLine,
  TimelineView,
  type UiLanguage,
} from "./components/conversation.js";
import {
  isPermissionMode,
  type JsonValue,
  type PermissionMode,
  type ServerFrame,
} from "./protocol.js";
import {
  appendLocal,
  appendUser,
  createInitialState,
  reduceFrame,
  setAllExpanded,
  toggleItem,
  type AppState,
} from "./state.js";
import { editorTheme } from "./theme.js";

export interface BridgePort {
  hello(): Promise<Record<string, JsonValue>>;
  startTurn(sessionKey: string, text: string): Promise<void>;
  cancelTurn(): Promise<void>;
  resolveApproval(approvalId: number, decision: ApprovalChoice): Promise<void>;
  newSession(sessionKey: string): Promise<void>;
  memoryCommand(payload: Record<string, JsonValue>): Promise<Record<string, JsonValue>>;
  setPermissionMode(mode: PermissionMode): Promise<PermissionMode>;
  shutdown(): Promise<void>;
  kill(): void;
  onEvent(handler: BridgeEventHandler): () => void;
  onFatal(handler: BridgeFatalHandler): () => void;
}

export interface ClipboardPort {
  copy(text: string): boolean;
}

export interface MiniClawTuiOptions {
  tui: TUI;
  bridge: BridgePort;
  clipboard?: ClipboardPort;
  language?: UiLanguage;
  sessionKey?: string;
}

export class MiniClawTui {
  public readonly tui: TUI;
  public readonly editor: Editor;
  public readonly timeline: TimelineView;
  public readonly scrollView: ScrollView;
  private readonly bridge: BridgePort;
  private readonly clipboard: ClipboardPort;
  private readonly header: HeaderLine;
  private readonly telemetry: TelemetryLine;
  private readonly root: VStack;
  private currentState = createInitialState();
  private currentLanguage: UiLanguage;
  private sessionKey: string;
  private contextBudget = 32_000;
  private permissionMode: PermissionMode = "safe";
  private approvalHandle: OverlayHandle | null = null;
  private approvalDialogValue: ApprovalDialog | null = null;
  private submittedDraft: string | null = null;
  private pendingOperations = new Set<Promise<unknown>>();
  private removeEvent?: () => void;
  private removeFatal?: () => void;
  private removeInput?: () => void;
  private exitResolve!: (code: number) => void;
  private readonly exitPromise: Promise<number>;
  private stopped = false;

  public constructor(options: MiniClawTuiOptions) {
    this.tui = options.tui;
    this.bridge = options.bridge;
    this.currentLanguage = options.language ?? "zh-CN";
    this.sessionKey = options.sessionKey ?? "default";
    this.clipboard = options.clipboard ?? systemClipboard(this.tui);
    this.header = new HeaderLine("0.1.0", "loading", this.sessionKey, "workspace", this.currentLanguage);
    this.timeline = new TimelineView(this.currentState, this.currentLanguage);
    this.scrollView = new ScrollView(this.timeline, {
      follow: "end",
      primary: true,
      overscroll: "contain",
      scrollbar: "auto",
    });
    this.telemetry = new TelemetryLine(
      this.currentState.telemetry,
      this.contextBudget,
      this.currentLanguage,
    );
    this.editor = new Editor(this.tui, editorTheme, { paddingX: 1 });
    this.root = new VStack(
      [
        { component: this.header, basis: "auto", minSize: 1 },
        { component: this.scrollView, basis: 0, grow: 1, minSize: 1 },
        { component: this.telemetry, basis: "auto", minSize: 1 },
        { component: this.editor, basis: "auto", minSize: 3, maxSize: 9 },
      ],
      { gap: 0 },
    );
    if (!isViewportTUI(this.tui)) {
      throw new BridgeRequestError("tui_mode", "MiniClaw pi-tui 需要 Alt Screen renderer");
    }
    this.tui.setLayoutRoot(this.root);
    this.exitPromise = new Promise((resolve) => {
      this.exitResolve = resolve;
    });
  }

  public get state(): AppState {
    return this.currentState;
  }

  public get language(): UiLanguage {
    return this.currentLanguage;
  }

  public get approvalDialog(): ApprovalDialog | null {
    return this.approvalDialogValue;
  }

  public async start(): Promise<void> {
    this.removeEvent = this.bridge.onEvent((frame) => this.onFrame(frame));
    this.removeFatal = this.bridge.onFatal((error) => {
      this.appendLocal(`${this.text("Core 已断开", "Core disconnected")}: ${error.code}`, "error");
      this.editor.disableSubmit = false;
    });
    this.removeInput = this.tui.addInputListener((data) => this.handleGlobalInput(data));
    this.editor.onSubmit = () => {};
    this.tui.setFocus(this.editor);
    this.tui.start();
    const metadata = await this.bridge.hello();
    const model = typeof metadata.model === "string" ? metadata.model : "unknown";
    const workspace = typeof metadata.workspace === "string" ? metadata.workspace : "workspace";
    const budget = metadata.context_budget_tokens;
    const permissionMode = metadata.permission_mode;
    this.contextBudget = typeof budget === "number" ? budget : this.contextBudget;
    if (isPermissionMode(permissionMode)) {
      this.permissionMode = permissionMode;
      this.header.setPermissionMode(permissionMode);
    }
    this.header.setMetadata(model, workspace);
    this.telemetry.setContextBudget(this.contextBudget);
    this.tui.requestRender(true);
  }

  public stop(code = 0): void {
    if (this.stopped) return;
    this.stopped = true;
    this.removeEvent?.();
    this.removeFatal?.();
    this.removeInput?.();
    this.approvalHandle?.hide();
    this.tui.stop();
    this.bridge.kill();
    this.exitResolve(code);
  }

  public waitForExit(): Promise<number> {
    return this.exitPromise;
  }

  public async whenIdle(): Promise<void> {
    while (this.pendingOperations.size > 0) {
      await Promise.allSettled([...this.pendingOperations]);
    }
  }

  public appendLocal(content: string, tone: "info" | "error" = "info"): void {
    this.applyState(appendLocal(this.currentState, content, tone));
  }

  public renderDocument(width: number): string[] {
    return [
      ...this.header.render(width),
      ...this.timeline.render(width),
      ...this.telemetry.render(width),
      ...this.editor.render(width),
    ];
  }

  private handleGlobalInput(data: string): { consume?: boolean } | undefined {
    if (matchesKey(data, Key.ctrl("o"))) {
      const expand = this.currentState.timeline.some(
        (item) => (item.kind === "reasoning" || item.kind === "tool") && !item.expanded,
      );
      this.applyState(setAllExpanded(this.currentState, expand));
      return { consume: true };
    }
    if (matchesKey(data, Key.ctrl("q"))) {
      this.track(this.close());
      return { consume: true };
    }
    if (matchesKey(data, Key.escape) && this.currentState.busy) {
      this.track(this.cancel());
      return { consume: true };
    }
    if (matchesKey(data, Key.enter) && this.approvalDialogValue === null) {
      this.track(this.submitExact());
      return { consume: true };
    }
    return undefined;
  }

  private async submitExact(): Promise<void> {
    if (this.editor.disableSubmit || this.currentState.pendingApproval !== null) return;
    const original = this.editor.getExpandedText();
    if (!original.trim()) return;
    this.editor.setText("");
    if (original.trimStart().startsWith("/")) {
      await this.runLocalCommand(original.trim());
      return;
    }
    this.applyState(appendUser(this.currentState, original));
    this.submittedDraft = original;
    this.editor.disableSubmit = true;
    try {
      await this.bridge.startTurn(this.sessionKey, original);
    } catch {
      this.restoreSubmittedDraft();
      this.editor.disableSubmit = false;
      this.appendLocal(this.text("本轮失败：Core 请求未完成。原输入已恢复。", "Turn failed. Draft restored."), "error");
    }
  }

  private async runLocalCommand(command: string): Promise<void> {
    const [name, ...arguments_] = command.split(/\s+/);
    const argument = arguments_[0];
    switch (name) {
      case "/copy":
        this.appendLocal(
          this.currentState.lastAssistantText && this.clipboard.copy(this.currentState.lastAssistantText)
            ? this.text("已复制最近回复。", "Copied the latest answer.")
            : this.text("没有可复制的回复。", "No answer to copy."),
        );
        break;
      case "/lang":
        if (argument === "en" || argument === "zh" || argument === "zh-CN") {
          this.setLanguage(argument === "en" ? "en" : "zh-CN");
        } else {
          this.appendLocal("用法: /lang zh | /lang en", "error");
        }
        break;
      case "/trace": {
        if (argument === "all") this.applyState(setAllExpanded(this.currentState, true));
        else if (argument === "compact") this.applyState(setAllExpanded(this.currentState, false));
        else if (argument && /^\d+$/.test(argument)) this.applyState(toggleItem(this.currentState, Number(argument)));
        else this.appendLocal("用法: /trace all | compact | <编号>", "error");
        break;
      }
      case "/clear":
        this.applyState(createInitialState());
        break;
      case "/new": {
        await this.bridge.memoryCommand({ action: "flush" });
        const next = `session-${Date.now().toString(36)}`;
        await this.bridge.newSession(next);
        this.sessionKey = next;
        this.header.setSession(next);
        this.applyState(createInitialState());
        break;
      }
      case "/status":
        this.appendLocal(this.statusText());
        break;
      case "/permissions":
        await this.changePermissionMode(argument);
        break;
      case "/memory":
        await this.runMemoryCommand(arguments_);
        break;
      case "/help":
        this.appendLocal("/copy · /lang zh|en · /trace all|compact|编号 · /permissions [safe|smart|autopilot|yolo] · /memory status|list|search|why|review|forget|approve|reject|flush · /status · /new · /clear · /quit");
        break;
      case "/quit":
      case "/exit":
        await this.close();
        break;
      default:
        this.appendLocal(this.text(`未知命令：${name}`, `Unknown command: ${name}`), "error");
    }
    this.tui.setFocus(this.editor);
    this.tui.requestRender();
  }

  private async runMemoryCommand(arguments_: string[]): Promise<void> {
    const [action, ...values] = arguments_;
    let payload: Record<string, JsonValue>;
    if (action === "status" || action === "flush") {
      payload = { action };
    } else if (action === "list" || action === "review") {
      const limit = values[0] && /^\d+$/.test(values[0]) ? Number(values[0]) : 20;
      payload = { action, limit };
    } else if (action === "search" && values.length > 0) {
      payload = { action, query: values.join(" "), limit: 10 };
    } else if ((action === "why" || action === "forget") && values.length === 1) {
      payload = { action, unit_id: values[0]! };
    } else if (
      (action === "approve" || action === "reject")
      && values.length === 2
      && /^\d+$/.test(values[0]!)
      && /^[0-9a-f]{64}$/.test(values[1]!)
    ) {
      payload = { action, review_id: Number(values[0]), preview_hash: values[1]! };
    } else {
      this.appendLocal("用法: /memory status | list [数量] | search <查询> | why <unit_id> | review [数量] | forget <unit_id> | approve|reject <review_id> <preview_hash> | flush", "error");
      return;
    }
    try {
      const response = await this.bridge.memoryCommand(payload);
      this.appendLocal(formatMemoryResponse(response));
    } catch (error) {
      const code = error instanceof BridgeRequestError ? error.code : "memory_command_failed";
      this.appendLocal(`${this.text("Memory 命令失败", "Memory command failed")}: ${code}`, "error");
    }
  }

  private onFrame(frame: ServerFrame): void {
    this.applyState(reduceFrame(this.currentState, frame));
    if (frame.type === "event.bridge_error") {
      const code = typeof frame.payload.code === "string" ? frame.payload.code : "core_operation_failed";
      this.restoreSubmittedDraft();
      this.editor.disableSubmit = false;
      this.tui.setFocus(this.editor);
      this.appendLocal(`${this.text("Core 操作失败", "Core operation failed")}: ${code}`, "error");
      return;
    }
    if (frame.type === "event.approval_required" && this.currentState.pendingApproval) {
      this.showApproval();
      return;
    }
    if (
      frame.type === "event.turn_finished" ||
      frame.type === "event.turn_failed" ||
      frame.type === "event.turn_cancelled"
    ) {
      if (frame.type === "event.turn_finished") {
        this.submittedDraft = null;
      } else {
        this.restoreSubmittedDraft();
      }
      this.editor.disableSubmit = false;
      this.tui.setFocus(this.editor);
    }
    if (frame.type === "event.turn_failed") {
      const code = typeof frame.payload.error_code === "string" ? frame.payload.error_code : "agent";
      this.appendLocal(`${this.text("本轮失败", "Turn failed")}: ${code}`, "error");
    }
  }

  private showApproval(): void {
    const approval = this.currentState.pendingApproval;
    if (!approval) return;
    this.editor.disableSubmit = true;
    const terminalRows = this.tui.terminal.rows;
    const terminalColumns = this.tui.terminal.columns;
    const overlayRows = Math.min(18, Math.max(8, terminalRows - 2));
    const overlayWidth = Math.min(84, Math.max(20, terminalColumns - 4));
    this.approvalDialogValue = new ApprovalDialog(
      approval,
      this.currentLanguage,
      (decision) => {
        this.track(this.resolveApproval(decision));
      },
      overlayRows,
    );
    this.approvalHandle = this.tui.showOverlay(this.approvalDialogValue, {
      width: overlayWidth,
      minWidth: Math.min(48, overlayWidth),
      maxHeight: overlayRows,
      anchor: "center",
      margin: 1,
    });
  }

  private async resolveApproval(decision: ApprovalChoice): Promise<void> {
    const approval = this.currentState.pendingApproval;
    if (!approval) return;
    this.approvalHandle?.hide();
    this.approvalHandle = null;
    this.approvalDialogValue = null;
    this.currentState = { ...this.currentState, pendingApproval: null, busy: true };
    this.applyState(this.currentState);
    try {
      await this.bridge.resolveApproval(approval.approvalId, decision);
    } catch {
      this.editor.disableSubmit = false;
      this.appendLocal(this.text("审批续跑失败。", "Approval continuation failed."), "error");
    }
  }

  private async cancel(): Promise<void> {
    try {
      await this.bridge.cancelTurn();
    } catch {
      this.appendLocal(this.text("取消请求未完成。", "Cancel request failed."), "error");
    } finally {
      this.editor.disableSubmit = false;
    }
  }

  private async close(): Promise<void> {
    try {
      await this.bridge.shutdown();
    } catch {
      // The process may already be gone; shutdown remains idempotent for the UI.
    }
    this.stop(0);
  }

  private setLanguage(language: UiLanguage): void {
    this.currentLanguage = language;
    this.header.setLanguage(language);
    this.telemetry.setLanguage(language);
    this.timeline.setLanguage(language);
    this.appendLocal(language === "zh-CN" ? "界面语言已切换为中文。" : "UI language changed to English.");
  }

  private async changePermissionMode(argument: string | undefined): Promise<void> {
    if (argument === undefined) {
      this.appendLocal(
        this.text(
          `当前权限模式：${this.permissionMode}`,
          `Current permission mode: ${this.permissionMode}`,
        ),
      );
      return;
    }
    if (!isPermissionMode(argument)) {
      this.appendLocal("用法: /permissions safe|smart|autopilot|yolo", "error");
      return;
    }
    try {
      const selected = await this.bridge.setPermissionMode(argument);
      this.permissionMode = selected;
      this.header.setPermissionMode(selected);
      this.appendLocal(
        this.text(
          `权限模式已切换为：${selected}`,
          `Permission mode changed to: ${selected}`,
        ),
      );
    } catch (error) {
      const code = error instanceof BridgeRequestError ? error.code : "permissions_change_failed";
      this.appendLocal(
        `${this.text("权限模式切换失败", "Permission mode change failed")}: ${code}`,
        "error",
      );
    }
  }

  private applyState(state: AppState): void {
    this.currentState = state;
    this.timeline.setState(state);
    this.telemetry.setTelemetry(state.telemetry);
    this.tui.requestRender();
  }

  private track<T>(operation: Promise<T>): void {
    this.pendingOperations.add(operation);
    void operation.then(
      () => this.pendingOperations.delete(operation),
      () => {
        this.pendingOperations.delete(operation);
        this.editor.disableSubmit = false;
        this.appendLocal(this.text("本地操作未完成。", "Local operation failed."), "error");
      },
    );
  }

  private restoreSubmittedDraft(): void {
    if (this.submittedDraft === null) return;
    const current = this.editor.getExpandedText();
    this.editor.setText(current ? `${this.submittedDraft}\n${current}` : this.submittedDraft);
    this.submittedDraft = null;
  }

  private statusText(): string {
    const telemetry = this.currentState.telemetry;
    if (this.currentLanguage === "zh-CN") {
      return `权限 ${this.permissionMode} · 上下文 ${telemetry.contextTokens ?? "N/A"}/${this.contextBudget} · 输入 ${telemetry.inputTokens ?? "N/A"} · 输出 ${telemetry.outputTokens ?? "N/A"} · 工具 ${telemetry.toolCalls} · 迭代 ${telemetry.iterations} · 耗时 ${telemetry.durationMs ?? "N/A"} ms · 请求 ${telemetry.providerRequestId ?? "N/A"}`;
    }
    return `mode ${this.permissionMode} · context ${telemetry.contextTokens ?? "N/A"}/${this.contextBudget} · in ${telemetry.inputTokens ?? "N/A"} · out ${telemetry.outputTokens ?? "N/A"} · tools ${telemetry.toolCalls} · iter ${telemetry.iterations} · time ${telemetry.durationMs ?? "N/A"} ms · request ${telemetry.providerRequestId ?? "N/A"}`;
  }

  private text(chinese: string, english: string): string {
    return this.currentLanguage === "zh-CN" ? chinese : english;
  }
}

function formatMemoryResponse(response: Record<string, JsonValue>): string {
  const items = response.items;
  if (Array.isArray(items)) {
    if (items.length === 0) return "Memory: (empty)";
    return items
      .map((item) => {
        if (typeof item !== "object" || item === null || Array.isArray(item)) return "Memory: ?";
        const id = typeof item.unit_id === "string" ? item.unit_id : "?";
        const reviewId = typeof item.review_id === "number" ? `review#${item.review_id} ` : "";
        const text = typeof item.text === "string" ? item.text : "";
        const status = typeof item.status === "string" ? ` (${item.status})` : "";
        const preview = typeof item.preview_hash === "string" ? ` hash=${item.preview_hash}` : "";
        return `${reviewId}[${id}] ${text}${status}${preview}`;
      })
      .join("\n");
  }
  const item = response.item;
  if (typeof item === "object" && item !== null && !Array.isArray(item)) {
    const id = typeof item.unit_id === "string" ? item.unit_id : "?";
    const text = typeof item.text === "string" ? item.text : "";
    const sources = Array.isArray(item.source_message_ids)
      ? item.source_message_ids.join(",")
      : "";
    return `[${id}] ${text}\nsources=${sources}`;
  }
  if (typeof response.review_id === "number" && typeof response.preview_hash === "string") {
    const id = typeof response.unit_id === "string" ? response.unit_id : "?";
    const text = typeof response.text === "string" ? response.text : "";
    return `review#${response.review_id} [${id}] ${text}\nhash=${response.preview_hash}`;
  }
  return JSON.stringify(response, null, 2);
}

function systemClipboard(tui: TUI): ClipboardPort {
  return {
    copy(text: string): boolean {
      if (!text) return false;
      if (process.platform === "darwin") {
        const result = spawnSync("/usr/bin/pbcopy", [], {
          input: text,
          encoding: "utf8",
          shell: false,
          timeout: 2_000,
        });
        return result.status === 0;
      }
      tui.terminal.write(`\u001b]52;c;${Buffer.from(text, "utf8").toString("base64")}\u0007`);
      return true;
    },
  };
}
