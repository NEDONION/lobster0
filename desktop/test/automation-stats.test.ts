import { describe, expect, it } from "vitest";

import type { AutomationSummary } from "../src/common/api";
import {
  SCHEDULE_FORM_KINDS,
  automationStats,
  scheduleDescription,
  scheduleFormSpec,
} from "../src/renderer/automation-stats";

function task(status: string, scheduleKind = "cron"): AutomationSummary {
  return {
    taskId: 1,
    name: "t",
    status,
    scheduleKind,
    scheduleExpression: "0 9 * * *",
    nextRunAt: null,
  };
}

describe("automationStats", () => {
  it("counts an empty list as all zeros", () => {
    expect(automationStats([])).toEqual({ total: 0, active: 0, paused: 0, failed: 0 });
  });

  it("counts each bucket independently", () => {
    const stats = automationStats([
      task("active"),
      task("active"),
      task("paused"),
      task("failed"),
      task("cancelled"),
    ]);
    expect(stats.total).toBe(5);
    expect(stats.active).toBe(2);
    expect(stats.paused).toBe(1);
    expect(stats.failed).toBe(1);
  });

  it("keeps unknown statuses in total without inventing a bucket", () => {
    // Core 未来新增状态时，总数仍要正确，不能被静默丢弃。
    const stats = automationStats([task("some_future_status")]);
    expect(stats.total).toBe(1);
    expect(stats.active + stats.paused + stats.failed).toBe(0);
  });
});

describe("scheduleDescription", () => {
  it("renders interval seconds as human-readable durations", () => {
    expect(scheduleDescription("interval", "300")).toBe("每 5 分钟");
    expect(scheduleDescription("interval", "3600")).toBe("每 1 小时");
    expect(scheduleDescription("interval", "86400")).toBe("每 1 天");
  });

  it("passes cron expressions through with a label", () => {
    expect(scheduleDescription("cron", "0 9 * * *")).toBe("cron 0 9 * * *");
  });

  it("labels one-off and heartbeat schedules", () => {
    expect(scheduleDescription("once", "2026-08-12T09:00:00Z")).toContain("单次");
    // heartbeat 无法从界面创建，但既有任务仍要能正确显示。
    expect(scheduleDescription("heartbeat", "60")).toContain("心跳");
  });

  it("falls back to the raw expression when it cannot be parsed", () => {
    expect(scheduleDescription("interval", "not-a-number")).toBe("interval not-a-number");
    expect(scheduleDescription("unknown_kind", "x")).toBe("unknown_kind x");
  });
});

describe("scheduleFormKinds", () => {
  it("offers an explicit immediate option so nobody has to type a timestamp", () => {
    expect(SCHEDULE_FORM_KINDS.map((item) => item.id)).toEqual([
      "now",
      "once",
      "interval",
      "cron",
    ]);
    expect(SCHEDULE_FORM_KINDS[0]?.needsExpression).toBe(false);
  });

  it("maps the immediate option onto the Core's once schedule", () => {
    const spec = scheduleFormSpec("now", "", () => new Date("2026-08-12T09:00:00Z"));
    expect(spec.scheduleKind).toBe("once");
    // Core 只认带显式 offset 的 RFC 3339。
    expect(spec.expression).toMatch(/^\d{4}-\d{2}-\d{2}T[\d:.]+(Z|[+-]\d{2}:\d{2})$/);
  });

  it("passes the typed expression through for the other kinds", () => {
    expect(scheduleFormSpec("cron", "0 9 * * *").expression).toBe("0 9 * * *");
    expect(scheduleFormSpec("interval", "3600").expression).toBe("3600");
    expect(scheduleFormSpec("once", "2026-08-12T09:00:00+08:00").expression)
      .toBe("2026-08-12T09:00:00+08:00");
  });
});
