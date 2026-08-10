import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from "react";

import type {
  ApprovalDecision,
  DesktopBootstrap,
  SessionHistory,
} from "../common/api";
import { resolveComposerKeyAction } from "./composer-keys";
import { Markdown } from "./markdown";
import {
  appendDesktopUser,
  cancelDesktopTask,
  continueDesktopApproval,
  createDesktopTaskState,
  hydrateSession,
  reduceDesktopFrame,
  toggleDesktopItem,
  type DesktopTaskStatus,
} from "./task-state";

interface TaskWorkbenchProps {
  sessionKey: string;
  bootstrap: DesktopBootstrap | null;
  bootstrapError: string | null;
  initialHistory: SessionHistory | null;
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

export function workspaceBasename(workspace: string): string {
  const trimmed = workspace.replace(/[\\/]+$/, "");
  const separator = Math.max(trimmed.lastIndexOf("/"), trimmed.lastIndexOf("\\"));
  return separator === -1 ? trimmed : trimmed.slice(separator + 1);
}

export function TaskWorkbench({
  sessionKey,
  bootstrap,
  bootstrapError,
  initialHistory,
  onBusyChange,
}: TaskWorkbenchProps): React.JSX.Element {
  const [task, setTask] = useState(() => (
    initialHistory ? hydrateSession(initialHistory) : createDesktopTaskState(sessionKey)
  ));
  const [draft, setDraft] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [resolvingApproval, setResolvingApproval] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const timelineRef = useRef<HTMLDivElement>(null);

  useEffect(() => window.lobster0.onFrame((frame) => {
    setTask((current) => reduceDesktopFrame(current, frame));
  }), []);

  const pendingApproval = task.run.pendingApproval;

  useEffect(() => {
    const timeline = timelineRef.current;
    if (timeline) {
      timeline.scrollTop = timeline.scrollHeight;
    }
  }, [task.run.timeline, pendingApproval]);

  function toggleReasoning(id: number): void {
    setTask((current) => toggleDesktopItem(current, id));
  }
  const liveBusy = submitting || task.status === "running" || pendingApproval !== null;
  const disabled = liveBusy;

  useEffect(() => {
    onBusyChange(liveBusy);
  }, [liveBusy, onBusyChange]);

  async function submitDraft(): Promise<void> {
    if (disabled || bootstrap === null || draft.trim().length === 0) {
      return;
    }
    const text = draft;
    setSubmitting(true);
    setActionError(null);
    try {
      await window.lobster0.startTurn({ sessionKey, text });
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

  function onComposerKeyDown(event: KeyboardEvent<HTMLTextAreaElement>): void {
    const action = resolveComposerKeyAction({
      key: event.key,
      shiftKey: event.shiftKey,
      isComposing: event.nativeEvent.isComposing,
    });
    if (action === "send") {
      event.preventDefault();
      void submitDraft();
    }
  }

  async function cancel(): Promise<void> {
    setActionError(null);
    try {
      await window.lobster0.cancelTurn();
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
      await window.lobster0.resolveApproval(pendingApproval.approvalId, decision);
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
  const emptyTask = task.run.timeline.length === 0;

  const composer = (
    <form
      className="composer"
      onSubmit={(event: FormEvent<HTMLFormElement>) => {
        event.preventDefault();
        void submitDraft();
      }}
    >
      {bootstrapError || actionError ? (
        <p className="composer-error" role="alert">{actionError ?? bootstrapError}</p>
      ) : null}
      <textarea
        aria-label="消息内容"
        disabled={disabled || bootstrap === null}
        onChange={(event) => setDraft(event.target.value)}
        onKeyDown={onComposerKeyDown}
        placeholder={bootstrap ? "描述目标、背景和期望产物…" : "正在连接 Lobster0 Core…"}
        rows={emptyTask ? 5 : 3}
        value={draft}
      />
      <div className="composer-actions">
        <span>
          {bootstrap
            ? `Main Agent · ${bootstrap.model} · ${workspaceBasename(bootstrap.workspace)} · ${bootstrap.permissionMode}`
            : "本地 Core"}
        </span>
        {task.status === "running" || task.status === "waiting_approval" ? (
          <button className="button-secondary" onClick={() => void cancel()} type="button">
            停止
          </button>
        ) : null}
        <button
          className="button-primary"
          disabled={disabled || bootstrap === null || draft.trim().length === 0}
          type="submit"
        >
          {submitting ? "正在发送" : "发送"}
        </button>
      </div>
    </form>
  );

  return (
    <section className="task-layout" aria-label="对话工作台">
      <section className="conversation-panel" data-mode={emptyTask ? "empty" : "thread"}>
        <div className="conversation-header">
          <div>
            <span className="eyebrow">CONVERSATION</span>
            <h1>{emptyTask ? "新对话" : "当前对话"}</h1>
          </div>
          <span className="task-status" data-status={task.status}>{STATUS_LABELS[task.status]}</span>
        </div>

        {emptyTask ? (
          <div className="conversation-invite">
            <h2>今天想完成什么？</h2>
            <p>说明目标和期望结果，Lobster0 会在需要操作本地资源时请求审批。</p>
            {composer}
          </div>
        ) : (
        <>
        <div className="timeline" aria-live="polite" ref={timelineRef}>
          {task.run.timeline.map((item) => {
            if (item.kind === "user" || item.kind === "assistant") {
              return (
                <article className={`message message-${item.kind}`} key={item.id}>
                  <span>{item.kind === "user" ? "你" : "Lobster0"}</span>
                  {item.content ? <Markdown content={item.content} /> : <p>…</p>}
                </article>
              );
            }
            if (item.kind === "reasoning") {
              return (
                <article className="activity-item reasoning-item" data-expanded={item.expanded} key={item.id}>
                  <button
                    aria-expanded={item.expanded}
                    className="reasoning-toggle"
                    onClick={() => toggleReasoning(item.id)}
                    type="button"
                  >
                    <span className="reasoning-caret" aria-hidden="true">{item.expanded ? "▾" : "▸"}</span>
                    <span>思考</span>
                  </button>
                  {item.expanded ? <Markdown content={item.content} /> : null}
                </article>
              );
            }
            if (item.kind === "tool") {
              return (
                <article className="activity-item tool-activity" key={item.id}>
                  <span>{item.name}</span>
                  <Markdown content={item.summary} />
                  <small>{item.status}</small>
                </article>
              );
            }
            return (
              <article className="activity-item" key={item.id}>
                <span>提示</span>
                <Markdown content={item.content} />
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
        {composer}
        </>
        )}
      </section>
    </section>
  );
}
