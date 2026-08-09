import { describe, expect, it } from "vitest";

import {
  DesktopRequestError,
  registerDesktopIpc,
  validateAutomationListInput,
  validateApprovalInput,
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
});
