import type { AutomationSummary } from "../common/api";

export interface AutomationStats {
  total: number;
  active: number;
  paused: number;
  failed: number;
}

/**
 * 统计定时任务各状态的数量。
 *
 * 只识别三种需要单独展示的状态；Core 未来新增状态时仍计入 `total`，
 * 不会被静默丢弃，也不会硬塞进某个已有分桶。
 */
export function automationStats(tasks: readonly AutomationSummary[]): AutomationStats {
  const stats: AutomationStats = { total: tasks.length, active: 0, paused: 0, failed: 0 };
  for (const task of tasks) {
    if (task.status === "active") {
      stats.active += 1;
    } else if (task.status === "paused") {
      stats.paused += 1;
    } else if (task.status === "failed") {
      stats.failed += 1;
    }
  }
  return stats;
}

/** 把秒数转成"每 N 分钟/小时/天"，取能整除的最大单位。 */
function intervalText(seconds: number): string {
  if (seconds % 86_400 === 0) {
    return `每 ${seconds / 86_400} 天`;
  }
  if (seconds % 3_600 === 0) {
    return `每 ${seconds / 3_600} 小时`;
  }
  if (seconds % 60 === 0) {
    return `每 ${seconds / 60} 分钟`;
  }
  return `每 ${seconds} 秒`;
}

/**
 * 把 Core 的调度类型与表达式转成一句可读描述。
 *
 * 无法解析时原样回退为 `kind expression`——展示层不该猜，宁可显示原始值
 * 也不要给出可能错误的"人话"。
 */
export function scheduleDescription(kind: string, expression: string): string {
  const raw = `${kind} ${expression}`;
  if (kind === "interval" || kind === "heartbeat") {
    const seconds = Number(expression);
    if (!Number.isInteger(seconds) || seconds <= 0) {
      return raw;
    }
    return kind === "heartbeat" ? `心跳 ${intervalText(seconds)}` : intervalText(seconds);
  }
  if (kind === "cron") {
    return `cron ${expression}`;
  }
  if (kind === "once") {
    const at = new Date(expression);
    if (Number.isNaN(at.getTime())) {
      return raw;
    }
    return `单次 ${at.toLocaleString("zh-CN")}`;
  }
  return raw;
}
