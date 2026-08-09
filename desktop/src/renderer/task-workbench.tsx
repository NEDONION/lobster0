import { useEffect, useState, type FormEvent } from "react";

import type {
  ApprovalDecision,
  DesktopBootstrap,
  SessionHistory,
  SessionSummary,
} from "../common/api";
import {
  appendDesktopUser,
  cancelDesktopTask,
  continueDesktopApproval,
  createDesktopTaskState,
  hydrateSession,
  reduceDesktopFrame,
  type DesktopTaskStatus,
} from "./task-state";

interface TaskWorkbenchProps {
  sessionKey: string;
  bootstrap: DesktopBootstrap | null;
  bootstrapError: string | null;
  initialHistory: SessionHistory | null;
  sessions: SessionSummary[];
  onSelectSession: (sessionKey: string) => void;
  onBusyChange: (busy: boolean) => void;
}

const STATUS_LABELS: Record<DesktopTaskStatus, string> = {
  idle: "等待开始",
  running: "运行中",
  waiting_approval: "等待审批",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
  interrupted: "已中断",
};

const APPROVAL_LABELS: Record<ApprovalDecision, string> = {
  deny: "拒绝",
  once: "仅本次允许",
  session: "本任务允许",
  always: "始终允许",
};

export function TaskWorkbench({
  sessionKey,
  bootstrap,
  bootstrapError,
  initialHistory,
  sessions,
  onSelectSession,
  onBusyChange,
}: TaskWorkbenchProps): React.JSX.Element {
  const [task, setTask] = useState(() => (
    initialHistory ? hydrateSession(initialHistory) : createDesktopTaskState(sessionKey)
  ));
  const [draft, setDraft] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [resolvingApproval, setResolvingApproval] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  useEffect(() => window.miniclaw.onFrame((frame) => {
    setTask((current) => reduceDesktopFrame(current, frame));
  }), []);

  const pendingApproval = task.run.pendingApproval;
  const liveBusy = submitting || task.status === "running" || pendingApproval !== null;
  const disabled = liveBusy;

  useEffect(() => {
    onBusyChange(liveBusy);
  }, [liveBusy, onBusyChange]);

  async function submit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    if (disabled || draft.trim().length === 0) {
      return;
    }
    const text = draft;
    setSubmitting(true);
    setActionError(null);
    try {
      await window.miniclaw.startTurn({ sessionKey, text });
      setTask((current) => ({
        ...appendDesktopUser(current, text),
        status: "running",
      }));
      setDraft("");
    } catch {
      setActionError("任务未能开始，请检查本地 Core 配置。");
    } finally {
      setSubmitting(false);
    }
  }

  async function cancel(): Promise<void> {
    setActionError(null);
    try {
      await window.miniclaw.cancelTurn();
      setTask((current) => cancelDesktopTask(current));
    } catch {
      setActionError("取消请求未完成，请重试。");
    }
  }

  async function resolveApproval(decision: ApprovalDecision): Promise<void> {
    if (!pendingApproval) {
      return;
    }
    setResolvingApproval(true);
    setActionError(null);
    try {
      await window.miniclaw.resolveApproval(pendingApproval.approvalId, decision);
      setTask((current) => continueDesktopApproval(current));
    } catch {
      setActionError("审批未提交，当前任务仍在等待处理。");
    } finally {
      setResolvingApproval(false);
    }
  }

  const approvalChoices: ApprovalDecision[] = pendingApproval
    ? ["deny", ...pendingApproval.grantModes]
    : [];

  return (
    <section className="task-layout" aria-label="任务工作台">
      <aside className="task-list-panel">
        <div className="panel-heading">
          <span>任务</span>
          <strong>
            {sessions.some((item) => item.sessionKey === sessionKey)
              ? sessions.length
              : sessions.length + 1}
          </strong>
        </div>
        <div className="task-list-scroll">
          <button aria-current="page" className="task-row" data-active="true" type="button">
            <span className="task-row-dot" aria-hidden="true" />
            <span>
              <strong>{sessions.find((item) => item.sessionKey === sessionKey)?.title ?? "当前任务"}</strong>
              <small>{STATUS_LABELS[task.status]}</small>
            </span>
          </button>
          {sessions.filter((item) => item.sessionKey !== sessionKey).map((session) => (
            <button
              className="task-row"
              data-active="false"
              key={session.sessionKey}
              disabled={liveBusy}
              onClick={() => onSelectSession(session.sessionKey)}
              type="button"
            >
              <span className="task-row-dot" aria-hidden="true" />
              <span>
                <strong>{session.title}</strong>
                <small>{session.status}</small>
              </span>
            </button>
          ))}
        </div>
        <div className="task-context">
          <span>工作目录</span>
          <strong>{bootstrap?.workspace ?? "等待 Core"}</strong>
        </div>
      </aside>

      <section className="conversation-panel">
        <div className="conversation-header">
          <div>
            <span className="eyebrow">CURRENT TASK</span>
            <h1>当前任务</h1>
          </div>
          <span className="task-status" data-status={task.status}>{STATUS_LABELS[task.status]}</span>
        </div>

        <div className="timeline" aria-live="polite">
          {task.run.timeline.length === 0 ? (
            <div className="timeline-empty">
              <span>开始一个任务</span>
              <p>说明目标和期望结果，MiniClaw 会在需要操作本地资源时请求审批。</p>
            </div>
          ) : task.run.timeline.map((item) => {
            if (item.kind === "user" || item.kind === "assistant") {
              return (
                <article className={`message message-${item.kind}`} key={item.id}>
                  <span>{item.kind === "user" ? "你" : "MiniClaw"}</span>
                  <p>{item.content || "…"}</p>
                </article>
              );
            }
            if (item.kind === "reasoning") {
              return (
                <article className="activity-item" key={item.id}>
                  <span>思考</span>
                  <p>{item.content}</p>
                </article>
              );
            }
            if (item.kind === "tool") {
              return (
                <article className="activity-item tool-activity" key={item.id}>
                  <span>{item.name}</span>
                  <p>{item.summary}</p>
                  <small>{item.status}</small>
                </article>
              );
            }
            return (
              <article className="activity-item" key={item.id}>
                <span>提示</span>
                <p>{item.content}</p>
              </article>
            );
          })}

          {pendingApproval ? (
            <section className="approval-card" aria-label="任务审批">
              <span className="approval-kicker">需要你的确认</span>
              <h2>{pendingApproval.summary}</h2>
              <p>工具：{pendingApproval.toolName}</p>
              <div className="approval-actions">
                {approvalChoices.map((decision) => (
                  <button
                    disabled={resolvingApproval}
                    key={decision}
                    onClick={() => void resolveApproval(decision)}
                    type="button"
                  >
                    {APPROVAL_LABELS[decision]}
                  </button>
                ))}
              </div>
            </section>
          ) : null}
        </div>

        <form className="composer" onSubmit={(event) => void submit(event)}>
          {bootstrapError || actionError ? (
            <p className="composer-error" role="alert">{actionError ?? bootstrapError}</p>
          ) : null}
          <textarea
            aria-label="任务内容"
            disabled={disabled || bootstrap === null}
            onChange={(event) => setDraft(event.target.value)}
            placeholder={bootstrap ? "描述你想完成的事情…" : "正在连接 MiniClaw Core…"}
            rows={3}
            value={draft}
          />
          <div className="composer-actions">
            <span>{bootstrap ? `${bootstrap.model} · ${bootstrap.permissionMode}` : "本地 Core"}</span>
            {task.status === "running" || task.status === "waiting_approval" ? (
              <button className="button-secondary" onClick={() => void cancel()} type="button">
                取消
              </button>
            ) : null}
            <button
              className="button-primary"
              disabled={disabled || bootstrap === null || draft.trim().length === 0}
              type="submit"
            >
              {submitting ? "正在开始" : "开始任务"}
            </button>
          </div>
        </form>
      </section>

      <aside className="result-panel">
        <div className="panel-heading">
          <span>结果</span>
          <strong>{task.run.lastAssistantText ? "1" : "0"}</strong>
        </div>
        {task.run.lastAssistantText ? (
          <div className="result-content">
            <span>最终回复</span>
            <p>{task.run.lastAssistantText}</p>
          </div>
        ) : (
          <div className="result-empty">
            <span>R</span>
            <p>任务完成后，最终回复会集中在这里。</p>
          </div>
        )}
        <dl className="telemetry-list">
          <div><dt>工具调用</dt><dd>{task.run.telemetry.toolCalls}</dd></div>
          <div><dt>迭代</dt><dd>{task.run.telemetry.iterations}</dd></div>
          <div>
            <dt>耗时</dt>
            <dd>{task.run.telemetry.durationMs === null ? "—" : `${task.run.telemetry.durationMs} ms`}</dd>
          </div>
        </dl>
      </aside>
    </section>
  );
}
