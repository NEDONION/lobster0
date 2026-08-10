import { useEffect, useState } from "react";
import { isPermissionMode, type PermissionMode } from "@lobster0/pi-tui/protocol";

import type {
  AutomationList,
  DesktopBootstrap,
  SessionHistory,
  SessionSummary,
} from "../common/api";
import { NAV_ITEMS, type ViewId } from "./navigation";
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
    body: "查看 Lobster0 Core 已有的定时任务和下次运行时间。W1 仅提供只读信息。",
  },
  settings: {
    eyebrow: "LOCAL CONTROL",
    title: "设置",
    body: "查看本地 Core 配置，切换权限模式或选择新的工作目录。",
  },
};

const PERMISSION_MODES: readonly PermissionMode[] = ["safe", "smart", "autopilot", "yolo"];

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
  const [settingsError, setSettingsError] = useState<string | null>(null);
  const [settingsBusy, setSettingsBusy] = useState(false);
  const [taskBusy, setTaskBusy] = useState(false);
  const copy = VIEW_COPY[view];

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

  useEffect(() => {
    if (!bootstrap) {
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
  }, [bootstrap]);

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
          <span className="brand-mark" aria-hidden="true">M</span>
          <span>Lobster0</span>
        </div>
        <button
          className="sidebar-create"
          disabled={bootstrap === null || taskBusy}
          onClick={createTask}
          type="button"
        >
          <span className="nav-mark" aria-hidden="true">＋</span>
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
          <span className="sidebar-recent-heading">最近对话</span>
          {sessionsError ? (
            <p className="sidebar-recent-error" role="alert">{sessionsError}</p>
          ) : null}
          {sessions.length === 0 && !sessionsError ? (
            <p className="sidebar-recent-empty">还没有历史对话。</p>
          ) : (
            <div className="sidebar-recent-list">
              {sessions.map((session) => (
                <button
                  aria-current={session.sessionKey === sessionKey ? "page" : undefined}
                  data-active={session.sessionKey === sessionKey}
                  disabled={taskBusy}
                  key={session.sessionKey}
                  onClick={() => void openSession(session.sessionKey)}
                  type="button"
                >
                  <strong>{session.title}</strong>
                  <small>{session.status}</small>
                </button>
              ))}
            </div>
          )}
        </section>
        <div className="sidebar-status">
          <span className="pulse-dot" data-ready={bootstrap !== null} aria-hidden="true" />
          <span>{bootstrap ? "Core ready" : "Core offline"}</span>
          <strong>W1</strong>
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
            sessionKey={sessionKey}
          />
        ) : (
          <>
            <section className="intro" aria-labelledby={`${view}-title`}>
              <h1 id={`${view}-title`}>{copy.title}</h1>
              <p>{copy.body}</p>
            </section>
            <ViewPreview
              bootstrap={bootstrap}
              automations={automations}
              automationError={automationError}
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
  onChooseWorkspace,
  onSetPermissionMode,
}: {
  view: Exclude<ViewId, "task">;
  bootstrap: DesktopBootstrap | null;
  automations: AutomationList | null;
  automationError: string | null;
  settingsBusy: boolean;
  settingsError: string | null;
  taskBusy: boolean;
  onChooseWorkspace: () => void;
  onSetPermissionMode: (mode: PermissionMode) => void;
}): React.JSX.Element {
  if (view === "automation") {
    return (
      <section className="data-panel" aria-label="自动化列表">
        <div className="data-panel-heading">
          <span>调度状态</span>
          <strong data-enabled={automations?.enabled ?? false}>
            {automations?.enabled ? "运行中" : "未启用"}
          </strong>
        </div>
        {automationError ? <p className="panel-error" role="alert">{automationError}</p> : null}
        {automations?.tasks.length ? (
          <div className="automation-list">
            {automations.tasks.map((task) => (
              <article key={task.taskId}>
                <span className="automation-index">{String(task.taskId).padStart(2, "0")}</span>
                <div>
                  <strong>{task.name}</strong>
                  <small>{task.scheduleKind} · {task.status}</small>
                </div>
                <time>{task.nextRunAt
                  ? new Date(task.nextRunAt).toLocaleString("zh-CN")
                  : "无下次运行"}</time>
              </article>
            ))}
          </div>
        ) : (
          <p className="panel-empty">{automations ? "还没有自动化任务。" : "正在读取 Core…"}</p>
        )}
      </section>
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
        <div className="settings-row">
          <span>权限模式</span>
          <select
            aria-label="权限模式"
            disabled={!bootstrap || taskBusy || settingsBusy}
            onChange={(event) => {
              if (isPermissionMode(event.target.value)) {
                onSetPermissionMode(event.target.value);
              }
            }}
            value={bootstrap?.permissionMode ?? "safe"}
          >
            {PERMISSION_MODES.map((mode) => <option key={mode}>{mode}</option>)}
          </select>
        </div>
        <SettingsRow label="工具" value={bootstrap?.tools.join(" · ") || "未连接"} />
        <SettingsRow label="能力" value={bootstrap?.capabilities.join(" · ") || "未连接"} />
      </div>
      {taskBusy ? <p className="settings-note">任务运行或等待审批时不能修改本地设置。</p> : null}
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
