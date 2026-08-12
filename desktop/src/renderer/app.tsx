import { useEffect, useMemo, useState } from "react";
import type { PermissionMode } from "@lobster0/pi-tui/protocol";

import type {
  AutomationCreateInput,
  AutomationList,
  AutomationRun,
  DesktopBootstrap,
  ProviderList,
  ProviderUpsertInput,
  SessionHistory,
  SessionSummary,
} from "../common/api";
import { AutomationPanel } from "./automation-panel";
import { ModelsPanel } from "./models-panel";
import { NAV_ITEMS, type ViewId } from "./navigation";
import { PERMISSION_MODE_OPTIONS } from "./permission-modes";
import { SESSION_GROUP_LABELS, groupSessionsByRecency } from "./session-groups";
import { TaskWorkbench } from "./task-workbench";

const VIEW_COPY: Record<ViewId, { eyebrow: string; title: string; body: string }> = {
  task: {
    eyebrow: "CONVERSATION",
    title: "对话",
    body: "Python Core 接通后，这里会显示真实对话、工具过程和审批状态。",
  },
  automation: {
    eyebrow: "AUTOMATION",
    title: "自动化",
    body: "查看、暂停、立即运行或新建 Lobster0 Core 的定时任务。",
  },
  settings: {
    eyebrow: "LOCAL CONTROL",
    title: "设置",
    body: "查看本地 Core 配置，切换权限模式或选择新的工作目录。",
  },
};

// 侧栏历史列表只标注需要用户注意的状态。`completed` 是绝大多数会话的常态，
// 每条都标反而变成噪音，所以映射为 null 表示不展示。
const SESSION_STATUS_LABELS: Record<string, string | null> = {
  completed: null,
  running: "运行中",
  queued: "排队中",
  waiting_approval: "待审批",
  failed: "失败",
  cancelled: "已取消",
};

export function sessionStatusLabel(status: string): string | null {
  // 未知状态原样透出，便于发现 Core 新增的状态，而不是静默吞掉。
  return status in SESSION_STATUS_LABELS ? SESSION_STATUS_LABELS[status] ?? null : status;
}

export function App(): React.JSX.Element {
  const [view, setView] = useState<ViewId>("task");
  const [bootstrap, setBootstrap] = useState<DesktopBootstrap | null>(null);
  const [bootstrapError, setBootstrapError] = useState<string | null>(null);
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [sessionsError, setSessionsError] = useState<string | null>(null);
  const [sessionKey, setSessionKey] = useState<string>(() => crypto.randomUUID());
  const [history, setHistory] = useState<SessionHistory | null>(null);
  const [automations, setAutomations] = useState<AutomationList | null>(null);
  const [automationError, setAutomationError] = useState<string | null>(null);
  const [providers, setProviders] = useState<ProviderList | null>(null);
  const [providerError, setProviderError] = useState<string | null>(null);
  const [settingsError, setSettingsError] = useState<string | null>(null);
  const [settingsBusy, setSettingsBusy] = useState(false);
  const [taskBusy, setTaskBusy] = useState(false);
  const copy = VIEW_COPY[view];
  // 只在 Session 列表变化时重新分组；跨越午夜后的下一次列表刷新会自然更新分组。
  const sessionGroups = useMemo(() => groupSessionsByRecency(sessions, new Date()), [sessions]);

  useEffect(() => {
    let active = true;
    void window.lobster0.bootstrap().then((value) => {
      if (active) {
        setBootstrap(value);
        setBootstrapError(null);
      }
    }).catch(() => {
      if (active) {
        setBootstrapError("无法连接 Lobster0 Core，请检查本地启动配置。");
      }
    });
    return () => {
      active = false;
    };
  }, []);

  // 依赖里带上 taskBusy 与 sessionKey：回合结束是新会话与新标题出现的时刻，
  // 切换会话也要让高亮跟上。此前只依赖 bootstrap，整个生命周期只拉一次——
  // 侧栏因此永远停在应用启动那一刻的样子。
  useEffect(() => {
    if (!bootstrap || taskBusy) {
      return;
    }
    let active = true;
    void window.lobster0.listSessions(20).then((value) => {
      if (active) {
        setSessions(value);
        setSessionsError(null);
      }
    }).catch(() => {
      if (active) {
        setSessionsError("最近对话读取失败，请稍后重试。");
      }
    });
    return () => {
      active = false;
    };
  }, [bootstrap, taskBusy, sessionKey]);

  useEffect(() => {
    if (!bootstrap || view !== "automation") {
      return;
    }
    let active = true;
    void window.lobster0.listAutomations(50).then((value) => {
      if (active) {
        setAutomations(value);
        setAutomationError(null);
      }
    }).catch(() => {
      if (active) {
        setAutomationError("自动化列表读取失败，请稍后重试。");
      }
    });
    return () => {
      active = false;
    };
  }, [bootstrap, view]);

  useEffect(() => {
    if (!bootstrap || view !== "settings") {
      return;
    }
    let active = true;
    void window.lobster0.listProviders().then((value) => {
      if (active) {
        setProviders(value);
        setProviderError(null);
      }
    }).catch(() => {
      if (active) {
        setProviderError("Provider 列表读取失败，请稍后重试。");
      }
    });
    return () => {
      active = false;
    };
  }, [bootstrap, view]);

  function createTask(): void {
    if (taskBusy) {
      return;
    }
    setSessionKey(crypto.randomUUID());
    setHistory(null);
    setView("task");
  }

  async function openSession(selected: string): Promise<void> {
    if (taskBusy) {
      return;
    }
    setSessionsError(null);
    try {
      const loaded = await window.lobster0.loadSession(selected, 100);
      setSessionKey(selected);
      setHistory(loaded);
      setView("task");
    } catch {
      setSessionsError("对话历史读取失败，请稍后重试。");
    }
  }

  async function setPermissionMode(mode: PermissionMode): Promise<void> {
    if (!bootstrap || taskBusy) {
      return;
    }
    setSettingsBusy(true);
    setSettingsError(null);
    try {
      const selected = await window.lobster0.setPermissionMode(mode);
      setBootstrap({ ...bootstrap, permissionMode: selected });
    } catch {
      setSettingsError("权限模式切换失败，请确认当前没有运行中的任务。");
    } finally {
      setSettingsBusy(false);
    }
  }

  // Core 未开放写能力时只渲染只读列表，不显示会失败的按钮。
  const canWriteAutomation = bootstrap?.capabilities.includes("automation_write") ?? false;

  function refreshAutomations(): void {
    void window.lobster0.listAutomations(50).then((value) => {
      setAutomations(value);
      setAutomationError(null);
    }).catch(() => {
      setAutomationError("自动化列表读取失败，请稍后重试。");
    });
  }

  // Core 未开放写能力时只渲染只读列表，不显示会失败的按钮。
  const canWriteProviders = bootstrap?.capabilities.includes("providers_write") ?? false;

  function refreshProviders(): void {
    void window.lobster0.listProviders().then((value) => {
      setProviders(value);
      setProviderError(null);
    }).catch(() => {
      setProviderError("Provider 列表读取失败，请稍后重试。");
    });
  }

  /** 所有 Provider 写操作共用：失败只给固定文案，不透传可能带上密钥的异常文本。 */
  async function runProviderWrite(action: () => Promise<void>, failure: string): Promise<void> {
    setProviderError(null);
    try {
      await action();
    } catch {
      setProviderError(failure);
      return;
    }
    refreshProviders();
  }

  async function chooseWorkspace(): Promise<void> {
    if (!bootstrap || taskBusy) {
      return;
    }
    setSettingsBusy(true);
    setSettingsError(null);
    try {
      const selected = await window.lobster0.chooseWorkspace();
      if (selected === null) {
        return;
      }
      setBootstrap(await window.lobster0.bootstrap());
      setHistory(null);
      setSessions([]);
      setSessionKey(crypto.randomUUID());
    } catch {
      setSettingsError("工作目录切换失败，Lobster0 已尝试恢复原目录。");
    } finally {
      setSettingsBusy(false);
    }
  }

  return (
    <div className="app-shell min-h-screen">
      <div className="app-drag-region" aria-hidden="true" />
      <aside className="sidebar">
        <div className="brand" aria-label="Lobster0 Desktop">
          <span>Lobster0</span>
        </div>
        <button
          className="nav-item sidebar-create"
          disabled={bootstrap === null || taskBusy}
          onClick={createTask}
          type="button"
        >
          <span className="nav-mark" aria-hidden="true">+</span>
          <span>新建对话</span>
        </button>
        <nav className="navigation" aria-label="主导航">
          {NAV_ITEMS.map((item) => (
            <button
              className="nav-item"
              data-active={view === item.id}
              disabled={taskBusy && item.id !== "task"}
              key={item.id}
              onClick={() => setView(item.id)}
              type="button"
            >
              <span className="nav-mark" aria-hidden="true">{item.mark}</span>
              <span>{item.label}</span>
            </button>
          ))}
        </nav>
        <section className="sidebar-recent" aria-label="最近对话">
          {sessionsError ? (
            <p className="sidebar-recent-error" role="alert">{sessionsError}</p>
          ) : null}
          {sessions.length === 0 && !sessionsError ? (
            <p className="sidebar-recent-empty">还没有历史对话。</p>
          ) : (
            <div className="sidebar-recent-list">
              {sessionGroups.map((group) => (
                <div className="sidebar-recent-group" key={group.key}>
                  <span className="sidebar-recent-heading">
                    {SESSION_GROUP_LABELS[group.key]}
                  </span>
                  {group.sessions.map((session) => (
                    <button
                      aria-current={session.sessionKey === sessionKey ? "page" : undefined}
                      data-active={session.sessionKey === sessionKey}
                      disabled={taskBusy}
                      key={session.sessionKey}
                      onClick={() => void openSession(session.sessionKey)}
                      type="button"
                    >
                      <strong>{session.title}</strong>
                      {sessionStatusLabel(session.status) ? (
                        <small data-status={session.status}>
                          {sessionStatusLabel(session.status)}
                        </small>
                      ) : null}
                    </button>
                  ))}
                </div>
              ))}
            </div>
          )}
        </section>
        <div className="sidebar-status">
          <span className="pulse-dot" data-ready={bootstrap !== null} aria-hidden="true" />
          <span>{bootstrap ? "已连接本地 Core" : "未连接本地 Core"}</span>
        </div>
      </aside>

      <main className={`workspace ${view === "task" ? "workspace-task" : ""}`} data-view={view}>
        <header className="workspace-header">
          <span className="eyebrow">{copy.eyebrow}</span>
          <span className="local-badge">{bootstrap ? "Core 已连接" : "本地优先"}</span>
        </header>
        {view === "task" ? (
          <TaskWorkbench
            bootstrap={bootstrap}
            bootstrapError={bootstrapError}
            initialHistory={history}
            key={sessionKey}
            onBusyChange={setTaskBusy}
            onChooseWorkspace={() => void chooseWorkspace()}
            onOpenSettings={() => setView("settings")}
            sessionKey={sessionKey}
          />
        ) : (
          <>
            <section className="intro" aria-labelledby={`${view}-title`}>
              <h1 id={`${view}-title`}>{copy.title}</h1>
              <p>{copy.body}</p>
            </section>
            <ViewPreview
              canWriteAutomation={canWriteAutomation}
              onCancelAutomation={async (taskId) => {
                await window.lobster0.cancelAutomation(taskId);
              }}
              onOpenRun={(sessionKey) => void openSession(sessionKey)}
              onCreateAutomation={async (input: AutomationCreateInput) => {
                await window.lobster0.createAutomation(input);
              }}
              onHaltAutomation={async (reason) => {
                await window.lobster0.haltAutomation(reason);
              }}
              onLoadAutomationRuns={(taskId): Promise<AutomationRun[]> =>
                window.lobster0.listAutomationRuns(taskId)}
              onPauseAutomation={async (taskId) => {
                await window.lobster0.pauseAutomation(taskId);
              }}
              onRefreshAutomations={refreshAutomations}
              onResumeAutomation={async (taskId) => {
                await window.lobster0.resumeAutomation(taskId);
              }}
              onRunAutomation={async (taskId) => {
                await window.lobster0.runAutomation(taskId);
              }}
              onUnhaltAutomation={async () => {
                await window.lobster0.unhaltAutomation();
              }}
              bootstrap={bootstrap}
              automations={automations}
              automationError={automationError}
              canWriteProviders={canWriteProviders}
              onRefreshProviders={refreshProviders}
              onRemoveProvider={async (id) => {
                await runProviderWrite(
                  () => window.lobster0.removeProvider(id),
                  "删除 Provider 失败。",
                );
              }}
              onSelectProvider={async (id, model) => {
                await runProviderWrite(
                  () => window.lobster0.selectProvider(id, model),
                  "切换默认 Provider 失败。",
                );
              }}
              onSetProviderSecret={async (id, value) => {
                await runProviderWrite(
                  () => window.lobster0.setProviderSecret(id, value),
                  "密钥保存失败。",
                );
              }}
              onUpsertProvider={async (input) => {
                await runProviderWrite(
                  () => window.lobster0.upsertProvider(input),
                  "保存 Provider 失败。",
                );
              }}
              onRestartCore={async () => {
                setSettingsBusy(true);
                setSettingsError(null);
                try {
                  setBootstrap(await window.lobster0.restartCore());
                } catch {
                  setSettingsError("Core 重启失败，请检查本地配置。");
                } finally {
                  setSettingsBusy(false);
                }
              }}
              providerError={providerError}
              providers={providers}
              onChooseWorkspace={() => void chooseWorkspace()}
              onSetPermissionMode={(mode) => void setPermissionMode(mode)}
              settingsBusy={settingsBusy}
              settingsError={settingsError}
              taskBusy={taskBusy}
              view={view}
            />
          </>
        )}
      </main>
    </div>
  );
}

function ViewPreview({
  view,
  bootstrap,
  automations,
  automationError,
  settingsBusy,
  settingsError,
  taskBusy,
  canWriteAutomation,
  canWriteProviders,
  providers,
  providerError,
  onRefreshProviders,
  onUpsertProvider,
  onRemoveProvider,
  onSelectProvider,
  onSetProviderSecret,
  onRestartCore,
  onChooseWorkspace,
  onSetPermissionMode,
  onRefreshAutomations,
  onPauseAutomation,
  onResumeAutomation,
  onCancelAutomation,
  onRunAutomation,
  onLoadAutomationRuns,
  onHaltAutomation,
  onUnhaltAutomation,
  onCreateAutomation,
  onOpenRun,
}: {
  view: Exclude<ViewId, "task">;
  bootstrap: DesktopBootstrap | null;
  automations: AutomationList | null;
  automationError: string | null;
  settingsBusy: boolean;
  canWriteAutomation: boolean;
  canWriteProviders: boolean;
  providers: ProviderList | null;
  providerError: string | null;
  onRefreshProviders: () => void;
  onUpsertProvider: (input: ProviderUpsertInput) => Promise<void>;
  onRemoveProvider: (id: string) => Promise<void>;
  onSelectProvider: (id: string, model: string) => Promise<void>;
  onSetProviderSecret: (id: string, value: string) => Promise<void>;
  onRestartCore: () => Promise<void>;
  onRefreshAutomations: () => void;
  onPauseAutomation: (taskId: number) => Promise<void>;
  onResumeAutomation: (taskId: number) => Promise<void>;
  onCancelAutomation: (taskId: number) => Promise<void>;
  onRunAutomation: (taskId: number) => Promise<void>;
  onLoadAutomationRuns: (taskId: number) => Promise<AutomationRun[]>;
  onHaltAutomation: (reason: string) => Promise<void>;
  onUnhaltAutomation: () => Promise<void>;
  onCreateAutomation: (input: AutomationCreateInput) => Promise<void>;
  onOpenRun: (sessionKey: string) => void;
  settingsError: string | null;
  taskBusy: boolean;
  onChooseWorkspace: () => void;
  onSetPermissionMode: (mode: PermissionMode) => void;
}): React.JSX.Element {
  if (view === "automation") {
    return (
      <AutomationPanel
        automationError={automationError}
        automations={automations}
        busy={taskBusy}
        canWrite={canWriteAutomation}
        onCancel={onCancelAutomation}
        onCreate={onCreateAutomation}
        onHalt={onHaltAutomation}
        onLoadRuns={onLoadAutomationRuns}
        onOpenRun={onOpenRun}
        onPause={onPauseAutomation}
        onRefresh={onRefreshAutomations}
        onResume={onResumeAutomation}
        onRun={onRunAutomation}
        onUnhalt={onUnhaltAutomation}
      />
    );
  }
  return (
    <section className="settings-panel" aria-label="本地设置">
      {settingsError ? <p className="panel-error" role="alert">{settingsError}</p> : null}
      <div className="settings-grid">
        <SettingsRow label="Core 版本" value={bootstrap?.coreVersion ?? "未连接"} />
        <SettingsRow label="模型" value={bootstrap?.model ?? "未连接"} />
        <SettingsRow label="工作目录" value={bootstrap?.workspace ?? "未连接"}>
          <button
            className="button-secondary"
            disabled={!bootstrap || taskBusy || settingsBusy}
            onClick={onChooseWorkspace}
            type="button"
          >
            选择目录
          </button>
        </SettingsRow>
        <div className="settings-row settings-row-modes">
          <span>权限模式</span>
          {/* 四个单词说明不了该选哪个，所以每档都直接把行为写在旁边，
              而不是塞进一个需要悬停才看得到的 title。 */}
          <div className="permission-modes" role="radiogroup" aria-label="权限模式">
            {PERMISSION_MODE_OPTIONS.map((option) => {
              const active = (bootstrap?.permissionMode ?? "safe") === option.mode;
              return (
                <button
                  aria-checked={active}
                  className="permission-mode"
                  data-active={active}
                  data-risky={option.risky}
                  disabled={!bootstrap || taskBusy || settingsBusy}
                  key={option.mode}
                  onClick={() => onSetPermissionMode(option.mode)}
                  role="radio"
                  type="button"
                >
                  <span className="permission-mode-label">
                    {option.label}
                    {option.risky ? <span className="permission-mode-flag">需谨慎</span> : null}
                  </span>
                  <span className="permission-mode-summary">{option.summary}</span>
                </button>
              );
            })}
          </div>
        </div>
        <SettingsRow label="工具" value={bootstrap?.tools.join(" · ") || "未连接"} />
        <SettingsRow label="能力" value={bootstrap?.capabilities.join(" · ") || "未连接"} />
      </div>
      {taskBusy ? <p className="settings-note">任务运行或等待审批时不能修改本地设置。</p> : null}
      <ModelsPanel
        busy={taskBusy || settingsBusy}
        canWrite={canWriteProviders}
        error={providerError}
        onRefresh={onRefreshProviders}
        onRemove={onRemoveProvider}
        onSelect={onSelectProvider}
        onRestartCore={onRestartCore}
        onSetSecret={onSetProviderSecret}
        onUpsert={onUpsertProvider}
        providers={providers}
      />
    </section>
  );
}

function SettingsRow({
  label,
  value,
  children,
}: {
  label: string;
  value: string;
  children?: React.ReactNode;
}): React.JSX.Element {
  return (
    <div className="settings-row">
      <span>{label}</span>
      <strong>{value}</strong>
      {children}
    </div>
  );
}
