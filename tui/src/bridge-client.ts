/** Child-process client for the Python Core NDJSON Bridge. */

import { spawn } from "node:child_process";
import type { Readable, Writable } from "node:stream";

import {
  NdjsonDecoder,
  ProtocolClientError,
  encodeRequest,
  type JsonValue,
  type PermissionMode,
  type RequestType,
  type ServerFrame,
  isPermissionMode,
} from "./protocol.js";

export interface BridgeProcess {
  stdin: Writable;
  stdout: Readable;
  stderr: Readable;
  kill(signal?: NodeJS.Signals): boolean;
  on(event: "exit", listener: (code: number | null, signal: NodeJS.Signals | null) => void): this;
}

export class BridgeRequestError extends Error {
  public readonly code: string;
  public readonly retryable: boolean;

  public constructor(code: string, message: string, retryable = false) {
    super(message);
    this.name = "BridgeRequestError";
    this.code = code;
    this.retryable = retryable;
  }
}

interface PendingRequest {
  resolve: (payload: Record<string, JsonValue>) => void;
  reject: (error: BridgeRequestError) => void;
}

export type BridgeEventHandler = (frame: ServerFrame) => void;
export type BridgeFatalHandler = (error: BridgeRequestError) => void;

export class BridgeClient {
  private readonly process: BridgeProcess;
  private readonly decoder = new NdjsonDecoder();
  private readonly pending = new Map<string, PendingRequest>();
  private readonly eventHandlers = new Set<BridgeEventHandler>();
  private readonly fatalHandlers = new Set<BridgeFatalHandler>();
  private sequence = 0;
  private closed = false;

  public constructor(process: BridgeProcess) {
    this.process = process;
    process.stdout.on("data", (chunk: Buffer | string) => this.consume(chunk));
    process.stdout.on("end", () => this.finishStream());
    process.stdout.on("error", () => this.failAll("bridge_stream", "Bridge 输出流失败"));
    process.stdin.on("error", () => this.failAll("bridge_stream", "Bridge 输入流失败"));
    process.stderr.resume();
    process.on("exit", (code, signal) => {
      const suffix = signal ? ` (${signal})` : code === null ? "" : ` (${code})`;
      this.failAll("bridge_exited", `MiniClaw Core 已退出${suffix}`);
    });
  }

  public static spawnFromEnvironment(environment: NodeJS.ProcessEnv = process.env): BridgeClient {
    const python = environment.MINICLAW_PYTHON?.trim();
    const home = environment.MINICLAW_HOME?.trim();
    if (!python || !home) {
      throw new BridgeRequestError("bridge_configuration", "缺少 Python Bridge 启动配置");
    }
    const child = spawn(python, ["-m", "miniclaw.bridge", "--home", home], {
      env: environment,
      shell: false,
      stdio: ["pipe", "pipe", "pipe"],
    });
    return new BridgeClient(child);
  }

  public onEvent(handler: BridgeEventHandler): () => void {
    this.eventHandlers.add(handler);
    return () => this.eventHandlers.delete(handler);
  }

  public onFatal(handler: BridgeFatalHandler): () => void {
    this.fatalHandlers.add(handler);
    return () => this.fatalHandlers.delete(handler);
  }

  public request(
    type: RequestType,
    payload: Record<string, JsonValue>,
  ): Promise<Record<string, JsonValue>> {
    if (this.closed) {
      return Promise.reject(
        new BridgeRequestError("bridge_closed", "MiniClaw Core 已关闭", true),
      );
    }
    this.sequence += 1;
    const id = `ui-${this.sequence}`;
    let line: string;
    try {
      line = encodeRequest(id, type, payload);
    } catch (error) {
      return Promise.reject(
        error instanceof ProtocolClientError
          ? new BridgeRequestError(error.code, error.message)
          : new BridgeRequestError("client_protocol", "无法编码 Bridge 请求"),
      );
    }
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.process.stdin.write(line, "utf8", (error) => {
        if (!error) {
          return;
        }
        const pending = this.pending.get(id);
        this.pending.delete(id);
        pending?.reject(new BridgeRequestError("bridge_stream", "无法写入 MiniClaw Core"));
      });
    });
  }

  public async hello(): Promise<Record<string, JsonValue>> {
    return this.request("client.hello", {
      client_name: "miniclaw-pi-tui",
      client_version: "0.1.0",
      protocols: [1],
    });
  }

  public async startTurn(sessionKey: string, text: string): Promise<void> {
    await this.request("turn.start", { session_key: sessionKey, text });
  }

  public async cancelTurn(): Promise<void> {
    await this.request("turn.cancel", {});
  }

  public async resolveApproval(
    approvalId: number,
    decision: "deny" | "once" | "session" | "always",
  ): Promise<void> {
    await this.request("approval.resolve", { approval_id: approvalId, decision });
  }

  public async newSession(sessionKey: string): Promise<void> {
    await this.request("session.new", { session_key: sessionKey });
  }

  public async setPermissionMode(mode: PermissionMode): Promise<PermissionMode> {
    const response = await this.request("permissions.set", { mode });
    const selected = response.permission_mode;
    if (!isPermissionMode(selected)) {
      throw new BridgeRequestError(
        "invalid_permission_mode",
        "MiniClaw Core 返回了无效权限模式",
      );
    }
    return selected;
  }

  public async shutdown(): Promise<void> {
    if (this.closed) {
      return;
    }
    await this.request("bridge.shutdown", {});
    this.closed = true;
  }

  public kill(): void {
    if (!this.closed) {
      this.closed = true;
      this.process.kill("SIGTERM");
    }
  }

  private consume(chunk: Buffer | string): void {
    try {
      for (const frame of this.decoder.push(
        typeof chunk === "string" ? Buffer.from(chunk) : chunk,
      )) {
        this.route(frame);
      }
    } catch (error) {
      const protocol =
        error instanceof ProtocolClientError
          ? new BridgeRequestError(error.code, error.message)
          : new BridgeRequestError("client_protocol", "Bridge 协议解析失败");
      this.failAll(protocol.code, protocol.message);
    }
  }

  private route(frame: ServerFrame): void {
    if (frame.type === "response.ok" || frame.type === "response.error") {
      if (!frame.id) {
        this.failAll("invalid_envelope", "Bridge 响应缺少请求 ID");
        return;
      }
      const pending = this.pending.get(frame.id);
      if (!pending) {
        return;
      }
      this.pending.delete(frame.id);
      if (frame.type === "response.ok") {
        pending.resolve(frame.payload);
        return;
      }
      pending.reject(
        new BridgeRequestError(
          typeof frame.payload.code === "string" ? frame.payload.code : "bridge_error",
          typeof frame.payload.message === "string" ? frame.payload.message : "Core 请求失败",
          frame.payload.retryable === true,
        ),
      );
      return;
    }
    if (frame.type.startsWith("event.")) {
      for (const handler of this.eventHandlers) {
        handler(frame);
      }
    }
  }

  private finishStream(): void {
    try {
      this.decoder.finish();
    } catch (error) {
      const code = error instanceof ProtocolClientError ? error.code : "truncated_frame";
      this.failAll(code, "Bridge 输出不完整");
    }
  }

  private failAll(code: string, message: string): void {
    if (this.closed && this.pending.size === 0) {
      return;
    }
    this.closed = true;
    const error = new BridgeRequestError(code, message);
    for (const pending of this.pending.values()) {
      pending.reject(error);
    }
    this.pending.clear();
    for (const handler of this.fatalHandlers) {
      handler(error);
    }
  }
}
