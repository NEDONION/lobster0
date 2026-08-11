import { useState } from "react";

import type { AutomationList, AutomationRun, AutomationSummary } from "../common/api";
import { automationStats, scheduleDescription } from "./automation-stats";

interface AutomationPanelProps {
  automations: AutomationList | null;
  automationError: string | null;
  /** Core 是否开放了写操作；未开放时只渲染只读列表。 */
  canWrite: boolean;
  /** 有回合在跑时禁用全部写操作，与 Core 的忙碌判定保持一致。 */
  busy: boolean;
  onRefresh: () => void;
  onPause: (taskId: number) => Promise<void>;
  onResume: (taskId: number) => Promise<void>;
  onCancel: (taskId: number) => Promise<void>;
  onRun: (taskId: number) => Promise<void>;
  onLoadRuns: (taskId: number) => Promise<AutomationRun[]>;
  onHalt: (reason: string) => Promise<void>;
  onUnhalt: () => Promise<void>;
  onCreate: (input: {
    name: string;
    prompt: string;
    scheduleKind: "once" | "interval" | "cron";
    expression: string;
  }) => Promise<void>;
}

const RUN_STATUS_LABELS: Record<string, string> = {
  succeeded: "成功",
  failed: "失败",
  running: "运行中",
  queued: "排队中",
  cancelled: "已取消",
};

const TASK_STATUS_LABELS: Record<string, string> = {
  active: "运行中",
  paused: "已暂停",
  failed: "失败",
  cancelled: "已取消",
};

export function AutomationPanel({
  automations,
  automationError,
  canWrite,
  busy,
  onRefresh,
  onPause,
  onResume,
  onCancel,
  onRun,
  onLoadRuns,
  onHalt,
  onUnhalt,
  onCreate,
}: AutomationPanelProps): React.JSX.Element {
  const [expandedRuns, setExpandedRuns] = useState<Record<number, AutomationRun[]>>({});
  const [creating, setCreating] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  const tasks = automations?.tasks ?? [];
  const stats = automationStats(tasks);

  async function guarded(action: () => Promise<void>): Promise<void> {
    setActionError(null);
    try {
      await action();
      onRefresh();
    } catch {
      setActionError("操作未完成，请稍后重试。");
    }
  }

  async function toggleRuns(taskId: number): Promise<void> {
    if (expandedRuns[taskId]) {
      setExpandedRuns((current) => {
        const next = { ...current };
        delete next[taskId];
        return next;
      });
      return;
    }
    setActionError(null);
    try {
      const runs = await onLoadRuns(taskId);
      setExpandedRuns((current) => ({ ...current, [taskId]: runs }));
    } catch {
      setActionError("运行历史读取失败。");
    }
  }

  // 破坏性操作一律先确认：取消不可逆、立即运行会真实消耗预算、
  // 急停会停掉所有自动化。
  function confirmed(message: string): boolean {
    return window.confirm(message);
  }

  return (
    <section className="automation-page" aria-label="自动化">
      {automations?.enabled === false ? (
        <p className="automation-halted" role="status">
          自动化调度当前未启用，已有任务不会被触发。
        </p>
      ) : null}
      {automationError ? <p className="panel-error" role="alert">{automationError}</p> : null}
      {actionError ? <p className="panel-error" role="alert">{actionError}</p> : null}

      <div className="automation-stats">
        {([
          ["总任务", stats.total],
          ["运行中", stats.active],
          ["已暂停", stats.paused],
          ["失败", stats.failed],
        ] as const).map(([label, value]) => (
          <div className="automation-stat" key={label}>
            <strong>{value}</strong>
            <span>{label}</span>
          </div>
        ))}
      </div>

      {canWrite ? (
        <div className="automation-toolbar">
          <button className="nav-item automation-action" onClick={onRefresh} type="button">
            刷新
          </button>
          <button
            className="nav-item automation-action"
            disabled={busy}
            onClick={() => setCreating((value) => !value)}
            type="button"
          >
            {creating ? "收起" : "＋ 新建任务"}
          </button>
          <button
            className="nav-item automation-action automation-danger"
            disabled={busy}
            onClick={() => {
              const reason = window.prompt("急停会停止所有自动化，请填写原因：");
              if (reason && reason.trim()) {
                void guarded(() => onHalt(reason));
              }
            }}
            type="button"
          >
            急停
          </button>
          <button
            className="nav-item automation-action"
            disabled={busy}
            onClick={() => void guarded(onUnhalt)}
            type="button"
          >
            解除急停
          </button>
        </div>
      ) : null}

      {creating ? (
        <AutomationCreateForm
          busy={busy}
          onCancel={() => setCreating(false)}
          onSubmit={async (input) => {
            await guarded(() => onCreate(input));
            setCreating(false);
          }}
        />
      ) : null}

      {tasks.length === 0 ? (
        <p className="panel-empty">{automations ? "还没有定时任务。" : "正在读取 Core…"}</p>
      ) : (
        <div className="automation-list">
          {tasks.map((task) => (
            <AutomationCard
              busy={busy}
              canWrite={canWrite}
              key={task.taskId}
              onCancel={() => {
                if (confirmed(`取消「${task.name}」后不可恢复，确定吗？`)) {
                  void guarded(() => onCancel(task.taskId));
                }
              }}
              onPause={() => void guarded(() => onPause(task.taskId))}
              onResume={() => void guarded(() => onResume(task.taskId))}
              onRun={() => {
                if (confirmed(`立即运行「${task.name}」会真实执行一次并消耗预算，确定吗？`)) {
                  void guarded(() => onRun(task.taskId));
                }
              }}
              onToggleRuns={() => void toggleRuns(task.taskId)}
              runs={expandedRuns[task.taskId]}
              task={task}
            />
          ))}
        </div>
      )}
    </section>
  );
}

function AutomationCard({
  task,
  runs,
  canWrite,
  busy,
  onPause,
  onResume,
  onCancel,
  onRun,
  onToggleRuns,
}: {
  task: AutomationSummary;
  runs: AutomationRun[] | undefined;
  canWrite: boolean;
  busy: boolean;
  onPause: () => void;
  onResume: () => void;
  onCancel: () => void;
  onRun: () => void;
  onToggleRuns: () => void;
}): React.JSX.Element {
  const paused = task.status === "paused";
  const terminal = task.status === "cancelled";
  return (
    <article className="automation-card">
      <div className="automation-card-main">
        <strong>{task.name}</strong>
        <small>
          {scheduleDescription(task.scheduleKind, task.scheduleExpression)}
          {" · "}
          {TASK_STATUS_LABELS[task.status] ?? task.status}
        </small>
        <time>
          {task.nextRunAt
            ? `下次 ${new Date(task.nextRunAt).toLocaleString("zh-CN")}`
            : "无下次运行"}
        </time>
      </div>
      {canWrite ? (
        <div className="automation-card-actions">
          <button
            className="nav-item automation-action"
            disabled={busy || terminal}
            onClick={paused ? onResume : onPause}
            type="button"
          >
            {paused ? "恢复" : "暂停"}
          </button>
          <button
            className="nav-item automation-action"
            disabled={busy || terminal}
            onClick={onRun}
            type="button"
          >
            立即运行
          </button>
          <button className="nav-item automation-action" onClick={onToggleRuns} type="button">
            {runs ? "收起历史" : "运行历史"}
          </button>
          <button
            className="nav-item automation-action automation-danger"
            disabled={busy || terminal}
            onClick={onCancel}
            type="button"
          >
            取消
          </button>
        </div>
      ) : null}
      {runs ? (
        <div className="automation-runs">
          {runs.length === 0 ? (
            <span>还没有运行记录。</span>
          ) : (
            runs.map((run) => (
              <div className="automation-run" key={run.runId}>
                <span>{new Date(run.scheduledFor).toLocaleString("zh-CN")}</span>
                <span data-status={run.status}>
                  {RUN_STATUS_LABELS[run.status] ?? run.status}
                </span>
                {run.errorCode ? <code>{run.errorCode}</code> : null}
              </div>
            ))
          )}
        </div>
      ) : null}
    </article>
  );
}

function AutomationCreateForm({
  busy,
  onCancel,
  onSubmit,
}: {
  busy: boolean;
  onCancel: () => void;
  onSubmit: (input: {
    name: string;
    prompt: string;
    scheduleKind: "once" | "interval" | "cron";
    expression: string;
  }) => Promise<void>;
}): React.JSX.Element {
  const [name, setName] = useState("");
  const [prompt, setPrompt] = useState("");
  const [kind, setKind] = useState<"once" | "interval" | "cron">("cron");
  const [expression, setExpression] = useState("0 9 * * *");
  const [error, setError] = useState<string | null>(null);

  const ready = name.trim().length > 0 && prompt.trim().length > 0 && expression.trim().length > 0;

  return (
    <form
      className="automation-form"
      onSubmit={(event) => {
        event.preventDefault();
        if (!ready) {
          return;
        }
        // 与 IPC/Core 一致的下限，避免提交明显会被拒绝的间隔。
        if (kind === "interval") {
          const seconds = Number(expression.trim());
          if (!Number.isInteger(seconds) || seconds < 300) {
            setError("间隔不能短于 5 分钟。");
            return;
          }
        }
        setError(null);
        void onSubmit({ name, prompt, scheduleKind: kind, expression });
      }}
    >
      <label>
        <span>任务名称</span>
        <input maxLength={64} onChange={(e) => setName(e.target.value)} value={name} />
      </label>
      <label>
        <span>执行内容</span>
        <textarea
          maxLength={4000}
          onChange={(e) => setPrompt(e.target.value)}
          placeholder="例如：汇总我昨天更新的飞书文档，发一份摘要"
          rows={3}
          value={prompt}
        />
      </label>
      <label>
        <span>调度方式</span>
        <select
          onChange={(e) => {
            const next = e.target.value as "once" | "interval" | "cron";
            setKind(next);
            setExpression(next === "cron" ? "0 9 * * *" : next === "interval" ? "3600" : "");
          }}
          value={kind}
        >
          <option value="cron">按 cron 表达式</option>
          <option value="interval">按固定间隔</option>
          <option value="once">仅执行一次</option>
        </select>
      </label>
      <label>
        <span>
          {kind === "cron" ? "cron 表达式" : kind === "interval" ? "间隔秒数（≥300）" : "执行时间"}
        </span>
        <input
          maxLength={200}
          onChange={(e) => setExpression(e.target.value)}
          placeholder={kind === "once" ? "2026-08-12T09:00:00+08:00" : ""}
          value={expression}
        />
      </label>
      {error ? <p className="panel-error" role="alert">{error}</p> : null}
      <div className="automation-form-actions">
        <button className="nav-item automation-action" onClick={onCancel} type="button">
          取消
        </button>
        <button className="button-primary" disabled={!ready || busy} type="submit">
          创建
        </button>
      </div>
    </form>
  );
}
