import { describe, expect, it } from "vitest";

import type { AutomationSummary } from "../src/common/api";
import {
  SCHEDULE_FORM_KINDS,
  automationActionError,
  automationStats,
  errorCodeFrom,
  groupRunsByParent,
  isTerminalTask,
  scheduleDescription,
  runDuration,
  runFailureReason,
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

describe("runFailureReason", () => {
  it("turns Core error codes into something a person can act on", () => {
    // 用户看到的曾经是 automation_terminal_response_missing 这一串英文。
    expect(runFailureReason("automation_terminal_response_missing")).toContain("收尾");
    expect(runFailureReason("schedule_misfire")).toContain("错过了预定时间");
    expect(runFailureReason("automation_halted")).toContain("急停");
  });

  it("falls back to the raw code instead of hiding it", () => {
    // 未知错误码也要露出来，否则用户和我都无从排查。
    expect(runFailureReason("some_new_code")).toContain("some_new_code");
  });

  it("returns null when the run did not fail", () => {
    expect(runFailureReason(null)).toBeNull();
  });
});

describe("runDuration", () => {
  it("reports how long the run actually took", () => {
    expect(runDuration("2026-08-11T17:40:54Z", "2026-08-11T17:41:49Z")).toBe("55 秒");
    expect(runDuration("2026-08-11T17:40:00Z", "2026-08-11T17:42:30Z")).toBe("2 分 30 秒");
  });

  it("says nothing when the run has not finished", () => {
    expect(runDuration("2026-08-11T17:40:54Z", null)).toBeNull();
    expect(runDuration(null, null)).toBeNull();
  });
});

describe("isTerminalTask", () => {
  it("treats a finished one-off task as terminal", () => {
    // 单次任务跑完即 completed，Core 拒绝对它做任何写操作（task_terminal）。
    // 界面此前只判 cancelled，于是给已完成的任务留着「取消」按钮，点了必然失败。
    expect(isTerminalTask("completed")).toBe(true);
    expect(isTerminalTask("cancelled")).toBe(true);
  });

  it("keeps active and paused tasks operable", () => {
    expect(isTerminalTask("active")).toBe(false);
    expect(isTerminalTask("paused")).toBe(false);
    expect(isTerminalTask("failed")).toBe(false);
  });
});

describe("automationActionError", () => {
  it("does not tell the user to retry something that can never succeed", () => {
    // task_terminal 是稳定状态，重试一万次也一样。
    const message = automationActionError("task_terminal");
    expect(message).not.toContain("重试");
    expect(message).toContain("已结束");
  });

  it("keeps the retry hint for genuinely transient failures", () => {
    expect(automationActionError("turn_busy")).toContain("稍后");
  });

  it("surfaces an unknown code instead of hiding it", () => {
    expect(automationActionError("some_new_code")).toContain("some_new_code");
  });
});

describe("errorCodeFrom", () => {
  it("recovers the code from an IPC-serialized error message", () => {
    // Electron 只把 message 送过 IPC，code 属性丢失；Main 侧把码写进 message，
    // 渲染进程再解析出来。不这么做，界面永远只能显示一句笼统的失败。
    const error = new Error(
      "Error invoking remote method 'desktop:automations:cancel': " +
        "BridgeRequestError: [task_terminal] 自动化操作未完成",
    );
    expect(errorCodeFrom(error)).toBe("task_terminal");
  });

  it("returns null when there is no code to find", () => {
    expect(errorCodeFrom(new Error("网络断开"))).toBeNull();
    expect(errorCodeFrom("not an error")).toBeNull();
  });
});

describe("groupRunsByParent", () => {
  const parent = {
    runId: 7, taskId: 1, status: "succeeded", scheduledFor: "2026-08-12T09:00:00Z",
    startedAt: null, completedAt: null, errorCode: null, resultPreview: null,
    inputTokens: null, outputTokens: null, sessionKey: null,
    parentRunId: null, subagentId: null,
  };
  const child = { ...parent, runId: 9, parentRunId: 7, subagentId: "researcher" };

  it("nests child runs under the run that dispatched them", () => {
    // 父子共用 task_id，平铺显示看不出谁派了谁。
    const groups = groupRunsByParent([child, parent]);

    expect(groups).toHaveLength(1);
    expect(groups[0]?.run.runId).toBe(7);
    expect(groups[0]?.children.map((item) => item.runId)).toEqual([9]);
  });

  it("keeps an orphaned child visible instead of dropping it", () => {
    // 父 Run 可能已超出 limit 被截断；丢掉子 Run 会让它彻底消失。
    const groups = groupRunsByParent([child]);

    expect(groups).toHaveLength(1);
    expect(groups[0]?.run.runId).toBe(9);
  });

  it("preserves the incoming order of parents", () => {
    const older = { ...parent, runId: 3 };
    const groups = groupRunsByParent([parent, older]);

    expect(groups.map((item) => item.run.runId)).toEqual([7, 3]);
  });
});
