import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type FormEvent,
  type KeyboardEvent,
} from "react";

import type { Telemetry } from "@lobster0/pi-tui/state";

import type {
  ApprovalDecision,
  ArtifactSummary,
  AttachmentRef,
  DesktopBootstrap,
  SessionHistory,
} from "../common/api";
import { ArtifactPanel } from "./artifact-panel";
import {
  addAttachment,
  attachmentIds,
  formatAttachmentSize,
  removeAttachment,
} from "./attachment-draft";
import { resolveComposerKeyAction } from "./composer-keys";
import { Markdown } from "./markdown";
import {
  appendDesktopUser,
  cancelDesktopTask,
  continueDesktopApproval,
  createDesktopTaskState,
  hydrateSession,
  reduceDesktopFrame,
  type DesktopTaskStatus,
} from "./task-state";
import { telemetryFacts } from "./telemetry-facts";
import { groupTimeline, toolDetail } from "./timeline-blocks";

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

/** 渲染某一条助手回复自己的运行指标；没有记录（历史会话、失败回合）时不占位。 */
function MessageTelemetry({ telemetry }: { telemetry: Telemetry | undefined }): React.JSX.Element | null {
  if (!telemetry) {
    return null;
  }
  const facts = telemetryFacts(telemetry);
  if (facts.length === 0) {
    return null;
  }
  return (
    <div className="message-telemetry">
      {facts.map((fact) => (
        <span key={fact.label} title={fact.label}>
          {fact.value}
        </span>
      ))}
    </div>
  );
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
  const [attachments, setAttachments] = useState<AttachmentRef[]>([]);
  const [staging, setStaging] = useState(false);
  const [artifacts, setArtifacts] = useState<ArtifactSummary[]>([]);
  const [artifactError, setArtifactError] = useState<string | null>(null);
  const [resolvingApproval, setResolvingApproval] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [expandedProcesses, setExpandedProcesses] = useState<ReadonlySet<number>>(new Set());
  const timelineRef = useRef<HTMLDivElement>(null);
  const timelineBlocks = useMemo(() => groupTimeline(task.run.timeline), [task.run.timeline]);

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

  // 过程块的折叠是纯展示层概念（由连续的思考/工具聚合而成），
  // 因此折叠状态放在本地，而不是 pi-tui 的逐条 expanded 字段上。
  function toggleProcess(id: number): void {
    setExpandedProcesses((current) => {
      const next = new Set(current);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
      return next;
    });
  }
  const liveBusy = submitting || task.status === "running" || pendingApproval !== null;
  const disabled = liveBusy;

  useEffect(() => {
    onBusyChange(liveBusy);
  }, [liveBusy, onBusyChange]);

  // 回合结束是新产物出现的时刻；会话切换也要重新拉，避免串到上一个会话。
  useEffect(() => {
    if (!bootstrap || liveBusy) {
      return;
    }
    let active = true;
    void window.lobster0.listArtifacts(sessionKey).then((value) => {
      if (active) {
        setArtifacts(value);
        setArtifactError(null);
      }
    }).catch(() => {
      if (active) {
        setArtifactError("产物列表读取失败。");
      }
    });
    return () => {
      active = false;
    };
  }, [bootstrap, sessionKey, liveBusy]);

  async function pickAndStageAttachment(): Promise<void> {
    setActionError(null);
    let path: string | null;
    try {
      path = await window.lobster0.pickAttachment();
    } catch {
      setActionError("无法打开文件选择器。");
      return;
    }
    if (path === null) {
      return;
    }
    setStaging(true);
    try {
      const staged = await window.lobster0.stageAttachment(path);
      setAttachments((current) => addAttachment(current, staged));
    } catch {
      setActionError("附件未通过校验：可能是类型不支持、体积过大或文件已变化。");
    } finally {
      setStaging(false);
    }
  }

  async function submitDraft(): Promise<void> {
    if (disabled || bootstrap === null || draft.trim().length === 0) {
      return;
    }
    const text = draft;
    const ids = attachmentIds(attachments);
    setSubmitting(true);
    setActionError(null);
    try {
      // 字段可选：没有附件时不能传空数组，Core 是 exact-key 校验。
      await window.lobster0.startTurn(
        ids.length > 0 ? { sessionKey, text, attachmentIds: ids } : { sessionKey, text },
      );
      setTask((current) => ({
        ...appendDesktopUser(current, text),
        status: "running",
      }));
      setDraft("");
      setAttachments([]);
    } catch {
      // 附件在 Core 侧一次性消费，失败后旧 id 已经作废，必须清掉重选。
      setAttachments([]);
      setActionError(
        ids.length > 0
          ? "任务未能开始，附件已失效，请重新添加后再发送。"
          : "任务未能开始，请检查本地 Core 配置。",
      );
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

  const canRevealArtifacts = bootstrap?.capabilities.includes("artifacts_read") ?? false;
  // Core 未开放附件能力时不显示入口，与 D2a/D2b 的做法一致。
  const canAttach = bootstrap?.capabilities.includes("attachments") ?? false;
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
      {attachments.length > 0 ? (
        <ul className="composer-attachments">
          {attachments.map((item) => (
            <li className="attachment-chip" key={item.artifactId}>
              <span className="attachment-name">{item.filename}</span>
              <span className="attachment-size">{formatAttachmentSize(item.sizeBytes)}</span>
              <button
                aria-label={`移除附件 ${item.filename}`}
                className="attachment-remove"
                disabled={disabled || staging}
                onClick={() =>
                  setAttachments((current) => removeAttachment(current, item.artifactId))}
                type="button"
              >
                ×
              </button>
            </li>
          ))}
        </ul>
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
        {canAttach ? (
          <button
            aria-label="添加附件"
            className="composer-attach"
            disabled={disabled || bootstrap === null || staging}
            onClick={() => void pickAndStageAttachment()}
            type="button"
          >
            {staging ? "正在校验…" : "📎"}
          </button>
        ) : null}
        <span>
          {bootstrap
            ? `Main Agent · ${bootstrap.model} · ${workspaceBasename(bootstrap.workspace)} · ${bootstrap.permissionMode}`
            : "本地 Core"}
        </span>
        {/* 运行中把「停止」提为主按钮并隐藏「发送」：跑飞的回合需要一眼能找到刹车，
            此时发送本来也是禁用的，留着只会挤占注意力。 */}
        {task.status === "running" || task.status === "waiting_approval" ? (
          <button className="button-stop" onClick={() => void cancel()} type="button">
            停止运行
          </button>
        ) : (
        <button
          className="button-primary"
          disabled={disabled || bootstrap === null || draft.trim().length === 0}
          type="submit"
        >
          {submitting ? "正在发送" : "发送"}
        </button>
        )}
      </div>
    </form>
  );

  return (
    <section className="task-layout" aria-label="对话工作台">
      <section className="conversation-panel" data-mode={emptyTask ? "empty" : "thread"}>
        {/* 空态下这条 header 会和居中的邀请标题重复，中间还留出一大片空白，
            所以只在已有对话时显示；空态由 conversation-invite 独占整个区域。 */}
        {/* 外层 workspace-header 已经有 CONVERSATION 标题，这里只补充本轮运行状态，
            不再重复渲染一遍标题（此前两处同时输出，界面上出现两个 CONVERSATION）。 */}
        {emptyTask ? null : (
          <div className="conversation-header">
            <span className="task-status" data-status={task.status}>
              {STATUS_LABELS[task.status]}
            </span>
          </div>
        )}

        {emptyTask ? (
          <div className="conversation-invite">
            <h2>今天想完成什么？</h2>
            <p>说明目标和期望结果，Lobster0 会在需要操作本地资源时请求审批。</p>
            {composer}
          </div>
        ) : (
        <>
        <div className="timeline" aria-live="polite" ref={timelineRef}>
          {timelineBlocks.map((block) => {
            if (block.kind === "message") {
              const { item } = block;
              return (
                <article className={`message message-${item.kind}`} key={block.id}>
                  <span>{item.kind === "user" ? "你" : "Lobster0"}</span>
                  {item.content ? <Markdown content={item.content} /> : <p>…</p>}
                  {item.kind === "assistant" ? (
                    <MessageTelemetry telemetry={task.turnTelemetry[item.turnId]} />
                  ) : null}
                </article>
              );
            }
            const expanded = expandedProcesses.has(block.id);
            return (
              <article className="process-block" data-expanded={expanded} key={block.id}>
                <button
                  aria-expanded={expanded}
                  className="process-toggle"
                  onClick={() => toggleProcess(block.id)}
                  type="button"
                >
                  <span className="process-caret" aria-hidden="true">{expanded ? "▾" : "▸"}</span>
                  <span>过程</span>
                  <span className="process-count">{block.items.length} 步</span>
                </button>
                {expanded ? (
                  <div className="process-items">
                    {block.items.map((item) => {
                      if (item.kind === "tool") {
                        const detail = toolDetail(item);
                        return (
                          <div className="process-item process-tool" key={item.id}>
                            <span className="process-tool-name">{item.name}</span>
                            {detail ? <span className="process-tool-detail">{detail}</span> : null}
                            <small>{item.status}</small>
                          </div>
                        );
                      }
                      return (
                        <div className="process-item" key={item.id}>
                          <span className="process-item-kind">
                            {item.kind === "reasoning" ? "思考" : "提示"}
                          </span>
                          <Markdown content={item.content} />
                        </div>
                      );
                    })}
                  </div>
                ) : null}
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
      {/* 有产物时右栏才出现，沿用 D1 的按需布局，不给空面板留位置。 */}
      {artifacts.length > 0 ? (
        <ArtifactPanel
          artifacts={artifacts}
          canReveal={canRevealArtifacts}
          error={artifactError}
          onPreview={(artifactId) => window.lobster0.previewArtifact(artifactId)}
          onReveal={(artifactId) => window.lobster0.revealArtifact(artifactId)}
        />
      ) : null}
    </section>
  );
}
