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

export type ScheduleFormKind = "now" | "once" | "interval" | "cron";

export interface ScheduleFormOption {
  id: ScheduleFormKind;
  label: string;
  /** 是否还需要用户填一个表达式；「立即执行」不需要。 */
  needsExpression: boolean;
  fieldLabel: string;
  placeholder: string;
  defaultExpression: string;
}

export const SCHEDULE_FORM_KINDS: readonly ScheduleFormOption[] = [
  {
    id: "now",
    label: "立即执行一次",
    needsExpression: false,
    fieldLabel: "",
    placeholder: "",
    defaultExpression: "",
  },
  {
    id: "once",
    label: "定时执行一次",
    needsExpression: true,
    fieldLabel: "执行时间",
    placeholder: "2026-08-12T09:00:00+08:00",
    defaultExpression: "",
  },
  {
    id: "interval",
    label: "按固定间隔重复",
    needsExpression: true,
    fieldLabel: "间隔秒数（≥300）",
    placeholder: "3600",
    defaultExpression: "3600",
  },
  {
    id: "cron",
    label: "按 cron 表达式",
    needsExpression: true,
    fieldLabel: "cron 表达式",
    placeholder: "0 9 * * *",
    defaultExpression: "0 9 * * *",
  },
];

/**
 * 把界面上的调度选择翻译成 Core 认识的 schedule。
 *
 * 「立即执行一次」在 Core 里没有对应类型，它就是一条时刻为「现在」的 once。
 * Core 创建时按配置的 misfire grace 容忍这点传输耗时。
 */
export function scheduleFormSpec(
  kind: ScheduleFormKind,
  expression: string,
  now: () => Date = () => new Date(),
): { scheduleKind: "once" | "interval" | "cron"; expression: string } {
  if (kind === "now") {
    return { scheduleKind: "once", expression: now().toISOString() };
  }
  return { scheduleKind: kind, expression };
}

// Core 的稳定错误码翻成人话。用户此前看到的就是这些英文串本身。
const RUN_FAILURE_REASONS: Record<string, string> = {
  automation_terminal_response_missing:
    "Agent 没有用工具收尾，本次结果未被采纳（产出仍可在完整过程里查看）",
  schedule_misfire: "错过了预定时间且已超出容错窗口，本次未执行",
  automation_halted: "自动化处于急停状态，本次未执行",
  automation_budget_exceeded: "超出本次运行的预算上限",
  automation_disabled: "自动化功能当前已关闭",
  turn_timeout: "执行超时",
};

export function runFailureReason(errorCode: string | null): string | null {
  if (!errorCode) {
    return null;
  }
  // 未知错误码也要露出来，否则用户和维护者都无从排查。
  return RUN_FAILURE_REASONS[errorCode] ?? `执行失败（${errorCode}）`;
}

export function runDuration(startedAt: string | null, completedAt: string | null): string | null {
  if (!startedAt || !completedAt) {
    return null;
  }
  const seconds = Math.round(
    (new Date(completedAt).getTime() - new Date(startedAt).getTime()) / 1000,
  );
  if (!Number.isFinite(seconds) || seconds < 0) {
    return null;
  }
  if (seconds < 60) {
    return `${seconds} 秒`;
  }
  return `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;
}

// 已终结的任务不能再被写操作：Core 会以 task_terminal 拒绝。单次任务跑完就是
// completed，此前界面只判 cancelled，于是给它留着「取消」按钮，点了必然失败。
const TERMINAL_TASK_STATUSES = new Set(["completed", "cancelled"]);

export function isTerminalTask(status: string): boolean {
  return TERMINAL_TASK_STATUSES.has(status);
}

// 只有真正瞬时的失败才提示「稍后重试」。对稳定状态说重试是误导。
const AUTOMATION_ACTION_ERRORS: Record<string, string> = {
  task_terminal: "这个任务已结束，无法再操作。",
  task_not_found: "任务不存在，可能已被删除。",
  task_version_conflict: "任务已被改动，请刷新后重试。",
  task_not_active: "任务当前不是运行状态，无法执行该操作。",
  system_task_immutable: "系统内建任务不可修改。",
  automation_halted: "自动化处于急停状态，请先解除急停。",
  turn_busy: "当前有任务正在运行，请稍后再试。",
};

export function automationActionError(errorCode: string | null): string {
  if (!errorCode) {
    return "操作未完成，请稍后重试。";
  }
  return AUTOMATION_ACTION_ERRORS[errorCode] ?? `操作未完成（${errorCode}）。`;
}

// Electron 只把 message 送过 IPC，Error 上的 code 属性会丢。Main 侧把码写成
// `[code]` 前缀，这里再解析回来——否则界面永远只能显示一句笼统的失败。
const ERROR_CODE_PATTERN = /\[([a-z][a-z0-9_]*)\]/;

export function errorCodeFrom(error: unknown): string | null {
  if (!(error instanceof Error)) {
    return null;
  }
  return ERROR_CODE_PATTERN.exec(error.message)?.[1] ?? null;
}
