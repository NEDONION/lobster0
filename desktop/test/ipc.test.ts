import { describe, expect, it } from "vitest";

import {
  DesktopRequestError,
  registerDesktopIpc,
  validateAutomationCreateInput,
  validateAutomationListInput,
  validateAutomationRunsInput,
  validateApprovalInput,
  validateHaltInput,
  validateArtifactIdInput,
  validateArtifactListInput,
  validateArtifactPreviewInput,
  validateAttachmentPathInput,
  validateProviderIdInput,
  validateProviderSecretInput,
  validateProviderSelectInput,
  validateProviderUpsertInput,
  validateTaskIdInput,
  validateHistoryInput,
  validateSessionListInput,
  validateStartTurnInput,
} from "../src/main/ipc";
import { DESKTOP_CHANNELS } from "../src/common/api";
import type { BridgeService } from "../src/main/bridge-service";

describe("产物查询的 Main 层校验", () => {
  it("bounds the artifact list", () => {
    expect(validateArtifactListInput({ sessionKey: "s", limit: 50 })).toEqual({
      sessionKey: "s",
      limit: 50,
    });
    for (const payload of [
      { sessionKey: "s", limit: 0 },
      { sessionKey: "s", limit: 501 },
      { sessionKey: "", limit: 10 },
      { sessionKey: "s" },
    ]) {
      expect(() => validateArtifactListInput(payload)).toThrowError(DesktopRequestError);
    }
  });

  it("rejects artifact ids outside the content-addressed shape", () => {
    const good = `art_${"a".repeat(64)}`;
    expect(validateArtifactIdInput({ artifactId: good })).toEqual({ artifactId: good });
    for (const artifactId of ["", "art_short", "../escape", `art_${"A".repeat(64)}`]) {
      expect(() => validateArtifactIdInput({ artifactId })).toThrowError(DesktopRequestError);
    }
  });

  it("bounds the preview size", () => {
    const good = `art_${"a".repeat(64)}`;
    expect(validateArtifactPreviewInput({ artifactId: good, maxBytes: 4096 })).toEqual({
      artifactId: good,
      maxBytes: 4096,
    });
    // 上限存在的意义是不让 Renderer 让 Core 读一个超大文件进内存。
    expect(() =>
      validateArtifactPreviewInput({ artifactId: good, maxBytes: 10_000_000 }),
    ).toThrowError(DesktopRequestError);
  });

  it("refuses a caller-supplied path on reveal", () => {
    // 路径只能由 Core 从 id 解析，否则等于开放任意本地路径的「在访达中显示」。
    const good = `art_${"a".repeat(64)}`;
    expect(() =>
      validateArtifactIdInput({ artifactId: good, path: "/etc/passwd" }),
    ).toThrowError(DesktopRequestError);
  });
});

describe("附件的 Main 层校验", () => {
  it("keeps the chosen absolute path and derives the declared type from the extension", () => {
    expect(validateAttachmentPathInput({ path: "/Users/x/Documents/note.txt" })).toEqual({
      path: "/Users/x/Documents/note.txt",
      declaredMediaType: "text/plain",
    });
    expect(validateAttachmentPathInput({ path: "/Users/x/shot.PNG" })).toEqual({
      path: "/Users/x/shot.PNG",
      declaredMediaType: "image/png",
    });
  });

  it("rejects relative paths and NUL bytes", () => {
    // 相对路径的含义取决于 Core 的 cwd，不可控。
    for (const path of ["note.txt", "", "~/note.txt", "/tmp/a\u0000b"]) {
      expect(() => validateAttachmentPathInput({ path })).toThrowError(DesktopRequestError);
    }
  });

  it("refuses extensions outside the Core media whitelist", () => {
    // 白名单由 Core 的 _MEDIA_EXTENSIONS 决定；这里提前拒绝，给出更快的反馈。
    for (const path of ["/tmp/app.dmg", "/tmp/script.sh", "/tmp/noext"]) {
      expect(() => validateAttachmentPathInput({ path })).toThrowError(DesktopRequestError);
    }
  });

  it("rejects a caller-supplied media type", () => {
    // 类型只能由扩展名推导，否则调用方可以谎报类型绕过前置检查。
    expect(() =>
      validateAttachmentPathInput({ path: "/tmp/a.txt", declaredMediaType: "image/png" }),
    ).toThrowError(DesktopRequestError);
  });
});

describe("Provider 配置的 Main 层校验", () => {
  it("accepts a well-formed upsert and rejects a caller-supplied env name", () => {
    expect(
      validateProviderUpsertInput({
        id: "openrouter",
        baseUrl: "https://openrouter.ai/api/v1",
        timeoutSeconds: 120,
      }),
    ).toEqual({ id: "openrouter", baseUrl: "https://openrouter.ai/api/v1", timeoutSeconds: 120 });
    // 密钥变量名由 Core 从 id 推导；接受调用方指定等于开放任意环境变量写入。
    expect(() =>
      validateProviderUpsertInput({
        id: "openrouter",
        baseUrl: "https://openrouter.ai/api/v1",
        timeoutSeconds: 120,
        apiKeyEnv: "PATH",
      }),
    ).toThrowError(DesktopRequestError);
  });

  it("rejects provider ids outside the safe character set", () => {
    for (const id of ["", "Upper", "has space", "../etc", "a".repeat(33), "-leading"]) {
      expect(() => validateProviderIdInput({ id })).toThrowError(DesktopRequestError);
    }
    expect(validateProviderIdInput({ id: "open-router_2" })).toEqual({ id: "open-router_2" });
  });

  it("rejects non-http base urls", () => {
    expect(() =>
      validateProviderUpsertInput({
        id: "x",
        baseUrl: "file:///etc/passwd",
        timeoutSeconds: 120,
      }),
    ).toThrowError(DesktopRequestError);
  });

  it("keeps the secret value byte-exact and never trims it away", () => {
    expect(validateProviderSecretInput({ id: "default", value: "sk-abc" })).toEqual({
      id: "default",
      value: "sk-abc",
    });
    // 空白、换行会破坏 dotenv 或注入第二个变量。
    for (const value of ["", "   ", "sk\nX=1", "sk\u0000"]) {
      expect(() => validateProviderSecretInput({ id: "default", value })).toThrowError(
        DesktopRequestError,
      );
    }
  });

  it("requires a model name when selecting a provider", () => {
    expect(validateProviderSelectInput({ id: "default", model: "gpt-5" })).toEqual({
      id: "default",
      model: "gpt-5",
    });
    expect(() => validateProviderSelectInput({ id: "default", model: "  " })).toThrowError(
      DesktopRequestError,
    );
  });
});

describe("Desktop Main IPC validation", () => {
  it("preserves valid task text exactly", () => {
    expect(validateStartTurnInput({ sessionKey: "task-1", text: "  整理报告  " })).toEqual({
      sessionKey: "task-1",
      text: "  整理报告  ",
    });
  });

  it("passes attachment ids through and rejects malformed ones", () => {
    expect(
      validateStartTurnInput({
        sessionKey: "task-1",
        text: "看看",
        attachmentIds: [`art_${"a".repeat(64)}`],
      }),
    ).toEqual({
      sessionKey: "task-1",
      text: "看看",
      attachmentIds: [`art_${"a".repeat(64)}`],
    });
    expect(() =>
      validateStartTurnInput({ sessionKey: "task-1", text: "看看", attachmentIds: ["../x"] }),
    ).toThrowError(DesktopRequestError);
  });

  it("rejects extra task fields at the Main trust boundary", () => {
    expect(() => validateStartTurnInput({
      sessionKey: "task-1",
      text: "整理报告",
      command: "rm",
    })).toThrowError(DesktopRequestError);
  });

  it("rejects boolean approval ids", () => {
    expect(() => validateApprovalInput({ approvalId: true, decision: "once" }))
      .toThrowError(DesktopRequestError);
  });

  it("accepts bounded Session queries and rejects extra Owner fields", () => {
    expect(validateSessionListInput({ limit: 20 })).toEqual({ limit: 20 });
    expect(validateHistoryInput({ sessionKey: "task-1", limit: 100 })).toEqual({
      sessionKey: "task-1",
      limit: 100,
    });
    expect(() => validateSessionListInput({ limit: 20, ownerId: 1 }))
      .toThrowError(DesktopRequestError);
    expect(() => validateHistoryInput({ sessionKey: "task-1", limit: 201 }))
      .toThrowError(DesktopRequestError);
  });

  it("accepts only bounded Automation list input", () => {
    expect(validateAutomationListInput({ limit: 50 })).toEqual({ limit: 50 });
    expect(() => validateAutomationListInput({ limit: true })).toThrowError(DesktopRequestError);
    expect(() => validateAutomationListInput({ limit: 101 })).toThrowError(DesktopRequestError);
  });

  it("returns null without restarting Core when folder selection is cancelled", async () => {
    const handlers = new Map<string, (payload: unknown) => Promise<unknown>>();
    const bridge = {
      onFrame: () => () => undefined,
      restartWorkspace: async () => {
        throw new Error("must not restart");
      },
    } as unknown as BridgeService;
    registerDesktopIpc(
      (channel, handler) => handlers.set(channel, handler),
      bridge,
      () => undefined,
      async () => null,
      async () => null,
    );

    await expect(handlers.get(DESKTOP_CHANNELS.workspaceChoose)?.(undefined))
      .resolves.toBeNull();
  });

  it("accepts only a positive integer task id", () => {
    expect(validateTaskIdInput({ taskId: 7 })).toEqual({ taskId: 7 });
    for (const payload of [
      { taskId: 0 },
      { taskId: -1 },
      { taskId: 1.5 },
      { taskId: "1" },
      { taskId: true },
      {},
      { taskId: 1, extra: 1 },
    ]) {
      expect(() => validateTaskIdInput(payload)).toThrowError(DesktopRequestError);
    }
  });

  it("requires a non-blank bounded halt reason", () => {
    expect(validateHaltInput({ reason: "刷屏了" })).toEqual({ reason: "刷屏了" });
    for (const payload of [{}, { reason: "" }, { reason: "   " }, { reason: "x".repeat(501) }]) {
      expect(() => validateHaltInput(payload)).toThrowError(DesktopRequestError);
    }
  });

  it("bounds the run-history limit", () => {
    expect(validateAutomationRunsInput({ taskId: 2, limit: 20 })).toEqual({
      taskId: 2,
      limit: 20,
    });
    for (const payload of [{ taskId: 2 }, { taskId: 2, limit: 0 }, { taskId: 2, limit: 101 }]) {
      expect(() => validateAutomationRunsInput(payload)).toThrowError(DesktopRequestError);
    }
  });

  it("narrows automation creation to schedule fields and enforces the interval floor", () => {
    expect(
      validateAutomationCreateInput({
        name: "每日摘要",
        prompt: "汇总昨天的文档",
        scheduleKind: "cron",
        expression: "0 9 * * *",
      }),
    ).toEqual({
      name: "每日摘要",
      prompt: "汇总昨天的文档",
      scheduleKind: "cron",
      expression: "0 9 * * *",
    });

    // timezone 是唯一可选字段
    expect(
      validateAutomationCreateInput({
        name: "n",
        prompt: "p",
        scheduleKind: "interval",
        expression: "300",
        timezone: "Asia/Shanghai",
      }).timezone,
    ).toBe("Asia/Shanghai");

    for (const payload of [
      // heartbeat 不允许从界面创建
      { name: "n", prompt: "p", scheduleKind: "heartbeat", expression: "60" },
      // 5 分钟下限，防止误配置高频空转
      { name: "n", prompt: "p", scheduleKind: "interval", expression: "299" },
      { name: "n", prompt: "p", scheduleKind: "interval", expression: "abc" },
      // 空白等同没填
      { name: "  ", prompt: "p", scheduleKind: "cron", expression: "* * * * *" },
      { name: "n", prompt: "  ", scheduleKind: "cron", expression: "* * * * *" },
      // 未开放字段一律拒绝，不能绕过界面收窄
      {
        name: "n",
        prompt: "p",
        scheduleKind: "cron",
        expression: "* * * * *",
        budget: { maxTurns: 999 },
      },
      { name: "n", prompt: "p", scheduleKind: "cron" },
    ]) {
      expect(() => validateAutomationCreateInput(payload)).toThrowError(DesktopRequestError);
    }
  });
});
