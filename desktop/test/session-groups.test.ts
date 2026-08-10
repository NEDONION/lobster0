import { describe, expect, it } from "vitest";

import {
  SESSION_GROUP_LABELS,
  groupSessionsByRecency,
  sessionGroupKey,
} from "../src/renderer/session-groups";
import type { SessionSummary } from "../src/common/api";

// 固定"现在"为本地时间 2026-08-11 12:00，避免测试随真实时钟漂移。
const NOW = new Date(2026, 7, 11, 12, 0, 0);

function session(sessionKey: string, updatedAt: string): SessionSummary {
  return { sessionKey, title: sessionKey, updatedAt, status: "completed" };
}

// 构造"本地时区的某个日历日 + 某小时"对应的 UTC ISO 串，
// 这样测试在任何时区运行都表达同一个本地日历日。
function localIso(daysAgo: number, hour: number): string {
  const date = new Date(2026, 7, 11 - daysAgo, hour, 30, 0);
  return date.toISOString();
}

describe("sessionGroupKey", () => {
  it("groups by local calendar day, not by elapsed hours", () => {
    // 本地今天凌晨 00:30 距now 不足 24h，昨天 23:30 也不足 24h，
    // 但它们分属不同日历日，必须落进不同分组。
    expect(sessionGroupKey(localIso(0, 0), NOW)).toBe("today");
    expect(sessionGroupKey(localIso(1, 23), NOW)).toBe("yesterday");
  });

  it("covers today, yesterday, week, month and earlier", () => {
    expect(sessionGroupKey(localIso(0, 9), NOW)).toBe("today");
    expect(sessionGroupKey(localIso(1, 9), NOW)).toBe("yesterday");
    expect(sessionGroupKey(localIso(2, 9), NOW)).toBe("week");
    expect(sessionGroupKey(localIso(6, 9), NOW)).toBe("week");
    expect(sessionGroupKey(localIso(7, 9), NOW)).toBe("month");
    expect(sessionGroupKey(localIso(29, 9), NOW)).toBe("month");
    expect(sessionGroupKey(localIso(30, 9), NOW)).toBe("earlier");
  });

  it("treats unparsable or empty timestamps as unknown instead of crashing", () => {
    expect(sessionGroupKey("", NOW)).toBe("unknown");
    expect(sessionGroupKey("not-a-date", NOW)).toBe("unknown");
  });

  it("does not put a future timestamp into earlier", () => {
    // 机器时钟回拨或 Core 时间超前时，不该被归类成"更早"。
    expect(sessionGroupKey(localIso(-1, 9), NOW)).toBe("today");
  });
});

describe("groupSessionsByRecency", () => {
  it("preserves Core ordering inside each group and drops empty groups", () => {
    const grouped = groupSessionsByRecency(
      [
        session("a", localIso(0, 10)),
        session("b", localIso(0, 9)),
        session("c", localIso(3, 9)),
      ],
      NOW,
    );
    expect(grouped.map((group) => group.key)).toEqual(["today", "week"]);
    expect(grouped[0]?.sessions.map((item) => item.sessionKey)).toEqual(["a", "b"]);
    expect(grouped[1]?.sessions.map((item) => item.sessionKey)).toEqual(["c"]);
  });

  it("keeps groups in recency order regardless of input order", () => {
    const grouped = groupSessionsByRecency(
      [session("old", localIso(40, 9)), session("new", localIso(0, 9))],
      NOW,
    );
    expect(grouped.map((group) => group.key)).toEqual(["today", "earlier"]);
  });

  it("returns an empty array for no sessions", () => {
    expect(groupSessionsByRecency([], NOW)).toEqual([]);
  });

  it("has a Chinese label for every group key it can emit", () => {
    const grouped = groupSessionsByRecency(
      [
        session("a", localIso(0, 9)),
        session("b", localIso(1, 9)),
        session("c", localIso(3, 9)),
        session("d", localIso(10, 9)),
        session("e", localIso(60, 9)),
        session("f", "broken"),
      ],
      NOW,
    );
    for (const group of grouped) {
      expect(SESSION_GROUP_LABELS[group.key]).toBeTruthy();
    }
  });
});
