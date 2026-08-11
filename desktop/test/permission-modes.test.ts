import { describe, expect, it } from "vitest";

import { PERMISSION_MODE_OPTIONS, permissionModeLabel } from "../src/renderer/permission-modes";

describe("permission mode options", () => {
  it("covers every mode the Core accepts", () => {
    expect(PERMISSION_MODE_OPTIONS.map((option) => option.mode)).toEqual([
      "safe",
      "smart",
      "autopilot",
      "yolo",
    ]);
  });

  it("gives every mode a display label and an explanation", () => {
    // 光给 safe/smart/autopilot/yolo 四个词，用户没法判断该选哪个。
    for (const option of PERMISSION_MODE_OPTIONS) {
      expect(option.label).toBe(option.mode.toUpperCase());
      expect(option.summary.length).toBeGreaterThan(6);
    }
  });

  it("warns explicitly on the two modes that stop asking", () => {
    const risky = PERMISSION_MODE_OPTIONS.filter((option) => option.risky).map((o) => o.mode);
    expect(risky).toEqual(["autopilot", "yolo"]);
  });

  it("falls back to the raw value for a mode it does not know", () => {
    // Core 未来新增模式时不能显示成空白。
    expect(permissionModeLabel("safe")).toBe("SAFE");
    expect(permissionModeLabel("some_future_mode")).toBe("SOME_FUTURE_MODE");
  });
});
