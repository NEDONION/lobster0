/** High-density conversation and activity components. */

import {
  Markdown,
  truncateToWidth,
  wrapTextWithAnsi,
  type Component,
} from "@earendil-works/pi-tui";

import type { AppState, Telemetry, TimelineItem, ToolItem } from "../state.js";
import { markdownTheme, palette, terminalSafe } from "../theme.js";

export type UiLanguage = "zh-CN" | "en";

export class HeaderLine implements Component {
  public constructor(
    private version: string,
    private model: string,
    private session: string,
    private workspace: string,
    private language: UiLanguage,
  ) {}

  public setMetadata(model: string, workspace: string): void {
    this.model = model;
    this.workspace = workspace;
  }

  public setSession(session: string): void {
    this.session = session;
  }

  public setLanguage(language: UiLanguage): void {
    this.language = language;
  }

  public invalidate(): void {}

  public render(width: number): string[] {
    const session = this.language === "zh-CN" ? "会话" : "session";
    const workspace = this.language === "zh-CN" ? "工作区" : "workspace";
    const text = palette.muted(
      ` MiniClaw ${this.version} · ${this.model} · ${session} ${this.session} · ${workspace} ${this.workspace}`,
    );
    return [truncateToWidth(text, Math.max(1, width), "")];
  }
}

export class TelemetryLine implements Component {
  public constructor(
    private telemetry: Telemetry,
    private contextBudget: number,
    private language: UiLanguage,
  ) {}

  public setTelemetry(telemetry: Telemetry): void {
    this.telemetry = telemetry;
  }

  public setContextBudget(contextBudget: number): void {
    this.contextBudget = contextBudget;
  }

  public setLanguage(language: UiLanguage): void {
    this.language = language;
  }

  public invalidate(): void {}

  public render(width: number): string[] {
    const zh = this.language === "zh-CN";
    const items = [
      `${zh ? "上下文" : "context"} ${metric(this.telemetry.contextTokens)}/${metric(this.contextBudget)}`,
      `${zh ? "输入" : "in"} ${metric(this.telemetry.inputTokens)}`,
      `${zh ? "输出" : "out"} ${metric(this.telemetry.outputTokens)}`,
      `${zh ? "工具" : "tools"} ${this.telemetry.toolCalls}`,
      `${zh ? "迭代" : "iter"} ${this.telemetry.iterations}`,
      `${zh ? "耗时" : "time"} ${duration(this.telemetry.durationMs)}`,
    ];
    return [truncateToWidth(palette.muted(` ${items.join(" · ")}`), Math.max(1, width), "")];
  }
}

export class TimelineView implements Component {
  public constructor(private state: AppState, private language: UiLanguage) {}

  public setState(state: AppState): void {
    this.state = state;
  }

  public setLanguage(language: UiLanguage): void {
    this.language = language;
  }

  public invalidate(): void {}

  public render(width: number): string[] {
    const safeWidth = Math.max(1, width);
    const lines: string[] = [];
    for (const item of this.state.timeline) {
      lines.push(...renderItem(item, safeWidth, this.language));
    }
    return lines.length > 0 ? lines : [palette.muted(this.language === "zh-CN" ? " 输入消息开始对话" : " Type a message to begin")];
  }
}

function renderItem(item: TimelineItem, width: number, language: UiLanguage): string[] {
  switch (item.kind) {
    case "user":
      return [
        palette.blue(" ▌ 你"),
        ...indent(terminalSafe(item.content), width, 3),
        "",
      ];
    case "assistant": {
      const role = palette.green(" ▌ MiniClaw");
      if (item.streaming) {
        return [role, ...indent(terminalSafe(item.content), width, 3), ""];
      }
      const markdown = new Markdown(
        terminalSafe(item.content),
        3,
        0,
        markdownTheme,
      );
      return [role, ...markdown.render(width), ""];
    }
    case "reasoning": {
      const title = language === "zh-CN" ? "思考（模型）" : "Reasoning (provider)";
      const lines = [palette.muted(` · ${item.expanded ? "▼" : "▶"} ${title} · 第 ${item.turnId} 轮`)];
      if (item.expanded) {
        lines.push(...indent(terminalSafe(item.content), width, 3, palette.muted));
      }
      return [...lines, ""];
    }
    case "tool":
      return renderTool(item, width, language);
    case "local":
      return [
        (item.tone === "error" ? palette.red : palette.muted)(` · ${terminalSafe(item.content)}`),
        "",
      ];
  }
}

function renderTool(item: ToolItem, width: number, language: UiLanguage): string[] {
  const successful = item.status === "succeeded";
  const failed = item.status === "failed" || item.status === "denied";
  const symbol = successful ? palette.green("✓") : failed ? palette.red("×") : palette.amber("…");
  const status = terminalSafe(item.status);
  const time = item.durationMs === null ? "" : ` · ${item.durationMs} ms`;
  const title = `${item.expanded ? "▼" : "▶"} ${language === "zh-CN" ? "工具" : "Tool"}: ${terminalSafe(item.name)} · ${status}${time} ${symbol}`;
  const lines = [palette.muted(` · ${title}`)];
  if (!item.expanded) {
    return [...lines, ""];
  }
  const lifecycle = item.lifecycle.join(" → ");
  lines.push(...indent(`${language === "zh-CN" ? "流程" : "lifecycle"}: ${lifecycle}`, width, 3, palette.muted));
  lines.push(...indent(`${language === "zh-CN" ? "摘要" : "summary"}: ${terminalSafe(item.summary)}`, width, 3));
  const argumentsText = terminalSafe(JSON.stringify(item.arguments, null, 2));
  lines.push(...indent(`${language === "zh-CN" ? "参数" : "arguments"}:\n${argumentsText}`, width, 3, palette.muted));
  if (item.preview) {
    lines.push(...indent(`${language === "zh-CN" ? "结果" : "result"}: ${terminalSafe(item.preview)}`, width, 3));
  }
  return [...lines, ""];
}

function indent(text: string, width: number, spaces: number, style?: (value: string) => string): string[] {
  const prefix = " ".repeat(Math.min(spaces, Math.max(0, width - 1)));
  const contentWidth = Math.max(1, width - prefix.length);
  const wrapped = text.split("\n").flatMap((line) => wrapTextWithAnsi(line || " ", contentWidth));
  return wrapped.map((line) => truncateToWidth(prefix + (style ? style(line) : line), width, ""));
}

function metric(value: number | null): string {
  if (value === null) {
    return "N/A";
  }
  if (value >= 1_000_000) {
    return `${trim(value / 1_000_000)}m`;
  }
  if (value >= 1_000) {
    return `${trim(value / 1_000)}k`;
  }
  return String(value);
}

function trim(value: number): string {
  return value.toFixed(1).replace(/\.0$/, "");
}

function duration(value: number | null): string {
  return value === null ? "N/A" : `${value} ms`;
}
