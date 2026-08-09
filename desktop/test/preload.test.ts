import { describe, expect, it } from "vitest";

import { createDesktopApi } from "../src/common/api";

describe("Desktop Preload API", () => {
  it("exposes only the approved fixed methods", () => {
    const api = createDesktopApi(async () => undefined, () => () => undefined);

    expect(Object.keys(api).sort()).toEqual([
      "bootstrap",
      "cancelTurn",
      "chooseWorkspace",
      "listAutomations",
      "listSessions",
      "loadSession",
      "onFrame",
      "resolveApproval",
      "setPermissionMode",
      "startTurn",
    ].sort());
  });

  it("maps a task start to its fixed IPC channel", async () => {
    const calls: unknown[][] = [];
    const api = createDesktopApi(async (...args) => {
      calls.push(args);
      return undefined;
    }, () => () => undefined);

    await api.startTurn({ sessionKey: "task-1", text: "整理报告" });

    expect(calls).toEqual([["desktop:task:start", {
      sessionKey: "task-1",
      text: "整理报告",
    }]]);
  });
});
