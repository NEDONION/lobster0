import { describe, expect, it } from "vitest";

import {
  DesktopRequestError,
  validateApprovalInput,
  validateHistoryInput,
  validateSessionListInput,
  validateStartTurnInput,
} from "../src/main/ipc";

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
});
