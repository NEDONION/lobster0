import { describe, expect, it } from "vitest";

import type { JsonValue, RequestType, ServerFrame } from "@miniclaw/pi-tui/protocol";

import { BridgeService } from "../src/main/bridge-service";

const HELLO = {
  protocol: 1,
  core_version: "0.1.0",
  model: "provider/model",
  workspace: "report",
  language: "zh-CN",
  context_budget_tokens: 32_000,
  permission_mode: "safe",
  tools: ["read_file"],
  capabilities: ["streaming", "approvals"],
};

class FakeClient {
  public readonly helloCalls: [string | undefined, string | undefined][] = [];
  public readonly turns: [string, string][] = [];
  public readonly requests: [RequestType, Record<string, JsonValue>][] = [];
  private eventHandler: ((frame: ServerFrame) => void) | undefined;

  public async hello(clientName?: string, clientVersion?: string): Promise<typeof HELLO> {
    this.helloCalls.push([clientName, clientVersion]);
    return HELLO;
  }

  public onEvent(handler: (frame: ServerFrame) => void): () => void {
    this.eventHandler = handler;
    return () => {
      this.eventHandler = undefined;
    };
  }

  public onFatal(): () => void {
    return () => undefined;
  }

  public async startTurn(sessionKey: string, text: string): Promise<void> {
    this.turns.push([sessionKey, text]);
  }

  public async request(
    type: RequestType,
    payload: Record<string, JsonValue>,
  ): Promise<Record<string, JsonValue>> {
    this.requests.push([type, payload]);
    if (type === "session.list") {
      return {
        sessions: [{
          session_key: "task-1",
          title: "整理报告",
          updated_at: "2026-08-09T00:00:00+00:00",
          status: "completed",
        }],
      };
    }
    return {
      session_key: "task-1",
      updated_at: "2026-08-09T00:00:00+00:00",
      turns: [{ turn_id: 7, status: "failed", error_code: "runtime_interrupted" }],
      messages: [{ role: "user", content: "整理报告", turn_id: 7 }],
    };
  }

  public async cancelTurn(): Promise<void> {}
  public async resolveApproval(): Promise<void> {}
  public async setPermissionMode(mode: "safe" | "smart" | "autopilot" | "yolo") {
    return mode;
  }
  public async shutdown(): Promise<void> {}
  public kill(): void {}

  public emit(frame: ServerFrame): void {
    this.eventHandler?.(frame);
  }
}

const TURN_STARTED = {
  v: 1 as const,
  type: "event.turn_started",
  payload: { turn_id: 7 },
};

describe("BridgeService", () => {
  it("handshakes as Desktop and returns validated Core bootstrap data", async () => {
    const client = new FakeClient();
    const service = new BridgeService(() => client, {});

    const bootstrap = await service.start();

    expect(client.helloCalls).toEqual([["miniclaw-desktop", "0.1.0"]]);
    expect(bootstrap).toEqual({
      coreVersion: "0.1.0",
      model: "provider/model",
      workspace: "report",
      language: "zh-CN",
      contextBudgetTokens: 32_000,
      permissionMode: "safe",
      tools: ["read_file"],
      capabilities: ["streaming", "approvals"],
    });
    expect(service.status).toBe("idle");
  });

  it("starts one task and forwards Core event frames", async () => {
    const client = new FakeClient();
    const service = new BridgeService(() => client, {});
    const frames: unknown[] = [];
    service.onFrame((frame) => frames.push(frame));
    await service.start();

    await service.startTurn({ sessionKey: "task-1", text: "整理报告" });
    client.emit(TURN_STARTED);

    expect(client.turns).toEqual([["task-1", "整理报告"]]);
    expect(service.status).toBe("running");
    expect(frames).toEqual([TURN_STARTED]);
  });

  it("moves through approval and returns to idle at a terminal event", async () => {
    const client = new FakeClient();
    const service = new BridgeService(() => client, {});
    await service.start();
    await service.startTurn({ sessionKey: "task-1", text: "整理报告" });

    client.emit({
      v: 1,
      type: "event.approval_required",
      payload: { turn_id: 7, approval_id: 9 },
    });
    expect(service.status).toBe("waiting_approval");

    await service.resolveApproval(9, "once");
    expect(service.status).toBe("running");

    client.emit({
      v: 1,
      type: "event.turn_finished",
      payload: { turn_id: 7, content: "完成" },
    });
    expect(service.status).toBe("idle");
  });

  it("maps bounded Core session responses to the Desktop API", async () => {
    const client = new FakeClient();
    const service = new BridgeService(() => client, {});
    await service.start();

    const sessions = await service.listSessions(20);
    const history = await service.loadSession("task-1", 100);

    expect(client.requests).toEqual([
      ["session.list", { limit: 20 }],
      ["session.history", { session_key: "task-1", limit: 100 }],
    ]);
    expect(sessions[0]).toEqual({
      sessionKey: "task-1",
      title: "整理报告",
      updatedAt: "2026-08-09T00:00:00+00:00",
      status: "completed",
    });
    expect(history.turns[0]).toEqual({
      turnId: 7,
      status: "failed",
      errorCode: "runtime_interrupted",
    });
  });
});
