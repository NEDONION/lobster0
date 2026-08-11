import { describe, expect, it } from "vitest";

import {
  DesktopRequestError,
  registerDesktopIpc,
  validateAutomationCreateInput,
  validateAutomationListInput,
  validateAutomationRunsInput,
  validateApprovalInput,
  validateHaltInput,
  validateTaskIdInput,
  validateHistoryInput,
  validateSessionListInput,
  validateStartTurnInput,
} from "../src/main/ipc";
import { DESKTOP_CHANNELS } from "../src/common/api";
import type { BridgeService } from "../src/main/bridge-service";

describe("Desktop Main IPC validation", () => {
  it("preserves valid task text exactly", () => {
    expect(validateStartTurnInput({ sessionKey: "task-1", text: "  整理报告  " })).toEqual({
      sessionKey: "task-1",
      text: "  整理报告  ",
    });
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
