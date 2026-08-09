import { describe, expect, it } from "vitest";

import type { ServerFrame } from "@miniclaw/pi-tui/protocol";

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
});
