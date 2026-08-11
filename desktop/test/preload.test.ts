import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";

import { createDesktopApi } from "../src/common/api";
import electronConfig from "../electron.vite.config";

describe("Desktop Preload API", () => {
  it("builds a CommonJS preload that Electron sandbox can execute", () => {
    expect(electronConfig).toMatchObject({
      preload: {
        build: {
          rollupOptions: {
            output: { entryFileNames: "[name].js", format: "cjs" },
          },
        },
      },
    });
  });

  it("ships an explicit Renderer content security policy", () => {
    const html = readFileSync(new URL("../index.html", import.meta.url), "utf8");
    expect(html).toContain('http-equiv="Content-Security-Policy"');
    expect(html).toContain("default-src 'self'");
  });

  it("exposes only the approved fixed methods", () => {
    const api = createDesktopApi(async () => undefined, () => () => undefined);

    expect(Object.keys(api).sort()).toEqual([
      "bootstrap",
      "cancelAutomation",
      "cancelTurn",
      "chooseWorkspace",
      "createAutomation",
      "haltAutomation",
      "listAutomationRuns",
      "listAutomations",
      "listSessions",
      "loadSession",
      "onFrame",
      "pauseAutomation",
      "resolveApproval",
      "resumeAutomation",
      "runAutomation",
      "setPermissionMode",
      "startTurn",
      "unhaltAutomation",
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
