import { describe, expect, it } from "vitest";

import type { JsonValue, RequestType, ServerFrame } from "@lobster0/pi-tui/protocol";

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
  automation_enabled: true,
};

class FakeClient {
  public readonly helloCalls: [string | undefined, string | undefined][] = [];
  public readonly turns: [string, string][] = [];
  public readonly requests: [RequestType, Record<string, JsonValue>][] = [];
  public shutdownCalls = 0;
  /** 覆盖某个请求类型的响应，供针对性用例使用。 */
  public readonly responses = new Map<RequestType, Record<string, JsonValue>>();
  private eventHandler: ((frame: ServerFrame) => void) | undefined;

  public constructor(
    private readonly helloPayload: typeof HELLO = HELLO,
    private readonly helloError: Error | null = null,
  ) {}

  public async hello(clientName?: string, clientVersion?: string): Promise<typeof HELLO> {
    this.helloCalls.push([clientName, clientVersion]);
    if (this.helloError) {
      throw this.helloError;
    }
    return this.helloPayload;
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
    const override = this.responses.get(type);
    if (override !== undefined) {
      return override;
    }
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
    if (type === "automation.list") {
      return {
        enabled: true,
        tasks: [{
          task_id: 4,
          name: "每日简报",
          status: "active",
          schedule_kind: "cron",
          schedule_expression: "0 1 * * *",
          next_run_at: "2026-08-10T01:00:00+00:00",
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
  public async shutdown(): Promise<void> {
    this.shutdownCalls += 1;
  }
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

    expect(client.helloCalls).toEqual([["lobster0-desktop", "0.1.0"]]);
    expect(bootstrap).toEqual({
      coreVersion: "0.1.0",
      model: "provider/model",
      workspace: "report",
      language: "zh-CN",
      contextBudgetTokens: 32_000,
      permissionMode: "safe",
      tools: ["read_file"],
      capabilities: ["streaming", "approvals"],
      automationEnabled: true,
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

  it("maps the bounded read-only Automation response", async () => {
    const client = new FakeClient();
    const service = new BridgeService(() => client, {});
    await service.start();

    await expect(service.listAutomations(50)).resolves.toEqual({
      enabled: true,
      tasks: [{
        taskId: 4,
        name: "每日简报",
        status: "active",
        scheduleKind: "cron",
        scheduleExpression: "0 1 * * *",
        nextRunAt: "2026-08-10T01:00:00+00:00",
      }],
    });
    expect(client.requests.at(-1)).toEqual(["automation.list", { limit: 50 }]);
  });

  it("restarts an idle Bridge with a selected workspace", async () => {
    const clients: FakeClient[] = [];
    const environments: NodeJS.ProcessEnv[] = [];
    const service = new BridgeService((environment) => {
      environments.push({ ...environment });
      const workspace = environment.LOBSTER0_WORKSPACE?.split("/").at(-1) ?? "report";
      const client = new FakeClient({ ...HELLO, workspace });
      clients.push(client);
      return client;
    }, { LOBSTER0_HOME: "/state/lobster0" });
    await service.start();

    const bootstrap = await service.restartWorkspace("/work/quarterly");

    expect(bootstrap.workspace).toBe("quarterly");
    expect(environments).toEqual([
      { LOBSTER0_HOME: "/state/lobster0" },
      { LOBSTER0_HOME: "/state/lobster0", LOBSTER0_WORKSPACE: "/work/quarterly" },
    ]);
    expect(clients[0]?.shutdownCalls).toBe(1);
  });

  it("rejects workspace restart while a task is running", async () => {
    const client = new FakeClient();
    const service = new BridgeService(() => client, {});
    await service.start();
    await service.startTurn({ sessionKey: "task-1", text: "整理报告" });

    await expect(service.restartWorkspace("/work/other"))
      .rejects.toMatchObject({ code: "bridge_state" });
    expect(client.shutdownCalls).toBe(0);
  });

  it("restores the previous Bridge configuration when the new workspace fails", async () => {
    const environments: NodeJS.ProcessEnv[] = [];
    let call = 0;
    const service = new BridgeService((environment) => {
      environments.push({ ...environment });
      call += 1;
      return call === 2
        ? new FakeClient(HELLO, new Error("new bridge failed"))
        : new FakeClient();
    }, { LOBSTER0_HOME: "/state/lobster0" });
    await service.start();

    await expect(service.restartWorkspace("/work/broken")).rejects.toThrow("new bridge failed");

    expect(service.status).toBe("idle");
    expect(environments).toEqual([
      { LOBSTER0_HOME: "/state/lobster0" },
      { LOBSTER0_HOME: "/state/lobster0", LOBSTER0_WORKSPACE: "/work/broken" },
      { LOBSTER0_HOME: "/state/lobster0" },
    ]);
  });
});

describe("loadSession 的消息映射", () => {
  it("接受只调了工具、正文为空的那一轮", async () => {
    // 那一轮往往正是关键一步；stringValue 拒绝空串，会让整个会话打不开。
    const client = new FakeClient();
    client.responses.set("session.history", {
      session_key: "task:1:run:1",
      updated_at: "2026-08-12T00:00:00Z",
      turns: [],
      messages: [
        { role: "assistant", content: "", turn_id: 309, tool_calls: ["http_get"] },
        { role: "tool", content: '{"data":{}}', turn_id: 309, tool_name: null },
      ],
    });
    const service = new BridgeService(() => client, {});
    await service.start();

    const history = await service.loadSession("task:1:run:1", 100);

    expect(history.messages).toHaveLength(2);
    expect(history.messages[0]?.content).toBe("");
    expect(history.messages[0]?.toolCalls).toEqual(["http_get"]);
    expect(history.messages[1]?.role).toBe("tool");
  });
});

describe("revealArtifact", () => {
  it("hands the path to the opener and never returns it to the renderer", async () => {
    // 路径是本机信息：Main 打开访达即可，Renderer 不需要也不该拿到。
    const client = new FakeClient();
    client.responses.set("artifacts.reveal", { path: "/Users/x/.lobster0/artifacts/a/b.png" });
    const opened: string[] = [];
    const service = new BridgeService(() => client, {}, undefined, (path) => {
      opened.push(path);
    });
    await service.start();

    const result = await service.revealArtifact(`art_${"a".repeat(64)}`);

    expect(opened).toEqual(["/Users/x/.lobster0/artifacts/a/b.png"]);
    expect(result).toBeUndefined();
  });

  it("refuses a response without a usable path", async () => {
    const client = new FakeClient();
    client.responses.set("artifacts.reveal", { path: "" });
    const service = new BridgeService(() => client, {}, undefined, () => undefined);
    await service.start();

    await expect(service.revealArtifact(`art_${"a".repeat(64)}`)).rejects.toThrow();
  });
});

describe("restartCore", () => {
  it("restarts without changing the workspace", async () => {
    // D2b 承诺改完 Provider/密钥能一键重启。restartWorkspace 已经是「停机再起」，
    // 这里只是不换目录的同一条路径。
    const clients: FakeClient[] = [];
    const service = new BridgeService(() => {
      const client = new FakeClient();
      clients.push(client);
      return client;
    }, { LOBSTER0_WORKSPACE: "/work/report" });
    await service.start();

    const bootstrap = await service.restartCore();

    expect(clients).toHaveLength(2);
    expect(clients[0]?.shutdownCalls).toBe(1);
    expect(bootstrap.coreVersion).toBe(HELLO.core_version);
  });

  it("refuses while a turn is running", async () => {
    // 回合跑到一半重启会丢掉正在进行的工作。
    const client = new FakeClient();
    const service = new BridgeService(() => client, {});
    await service.start();
    await service.startTurn({ sessionKey: "s", text: "hi" });

    await expect(service.restartCore()).rejects.toThrow();
  });
});
