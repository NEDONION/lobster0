import type { SessionSummary } from "../common/api";

export type SessionGroupKey =
  | "today"
  | "yesterday"
  | "week"
  | "month"
  | "earlier"
  | "unknown";

export interface SessionGroup {
  key: SessionGroupKey;
  sessions: SessionSummary[];
}

export const SESSION_GROUP_LABELS: Record<SessionGroupKey, string> = {
  today: "今天",
  yesterday: "昨天",
  week: "最近 7 天",
  month: "最近 30 天",
  earlier: "更早",
  unknown: "时间未知",
};

// 分组按最近优先固定排列，与 Core 返回的 updated_at DESC 顺序一致。
const GROUP_ORDER: readonly SessionGroupKey[] = [
  "today",
  "yesterday",
  "week",
  "month",
  "earlier",
  "unknown",
];

/** 把时间点归一化到它所在本地日历日的 00:00。 */
function startOfLocalDay(date: Date): number {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime();
}

/**
 * 判断一个会话属于哪个时间分组。
 *
 * Core 返回的是 UTC ISO 时间戳，但用户感知的“今天/昨天”是**本地日历日**，
 * 所以这里比较的是归一化到本地 00:00 之后的天数差，而不是经过的小时数——
 * 否则东八区凌晨的会话会被算成“昨天”。
 */
export function sessionGroupKey(updatedAt: string, now: Date): SessionGroupKey {
  const updated = new Date(updatedAt);
  if (Number.isNaN(updated.getTime())) {
    return "unknown";
  }
  const dayMs = 24 * 60 * 60 * 1000;
  const days = Math.round((startOfLocalDay(now) - startOfLocalDay(updated)) / dayMs);
  // 时钟回拨或 Core 时间超前时 days 为负，按最近处理而不是“更早”。
  if (days <= 0) {
    return "today";
  }
  if (days === 1) {
    return "yesterday";
  }
  if (days < 7) {
    return "week";
  }
  if (days < 30) {
    return "month";
  }
  return "earlier";
}

/**
 * 按时间分组会话，保留 Core 给出的组内顺序，并丢弃空分组。
 */
export function groupSessionsByRecency(
  sessions: readonly SessionSummary[],
  now: Date,
): SessionGroup[] {
  const buckets = new Map<SessionGroupKey, SessionSummary[]>();
  for (const session of sessions) {
    const key = sessionGroupKey(session.updatedAt, now);
    const bucket = buckets.get(key);
    if (bucket) {
      bucket.push(session);
    } else {
      buckets.set(key, [session]);
    }
  }
  return GROUP_ORDER.flatMap((key) => {
    const grouped = buckets.get(key);
    return grouped ? [{ key, sessions: grouped }] : [];
  });
}
