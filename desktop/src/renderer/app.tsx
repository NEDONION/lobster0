import { useEffect, useState } from "react";

import type {
  DesktopBootstrap,
  SessionHistory,
  SessionSummary,
} from "../common/api";
import { NAV_ITEMS, type ViewId } from "./navigation";
import { TaskWorkbench } from "./task-workbench";

const VIEW_COPY: Record<ViewId, { eyebrow: string; title: string; body: string }> = {
  home: {
    eyebrow: "GENERAL AGENT WORKSPACE",
    title: "把一件事，完整地交给 MiniClaw",
    body: "选择工作目录，描述目标，然后在同一个任务里查看过程、审批动作和最终结果。",
  },
  task: {
    eyebrow: "TASK WORKBENCH",
    title: "任务工作台",
    body: "Python Core 接通后，这里会显示真实对话、工具过程和审批状态。",
  },
  automation: {
    eyebrow: "AUTOMATION",
    title: "自动化",
    body: "W1 将从 MiniClaw Core 读取已有自动化；当前桌面壳不会展示虚构任务。",
  },
  settings: {
    eyebrow: "LOCAL CONTROL",
    title: "设置",
    body: "W1 将在这里显示真实模型、工作目录、工具能力和权限模式。",
  },
};

export function App(): React.JSX.Element {
  const [view, setView] = useState<ViewId>("home");
  const [bootstrap, setBootstrap] = useState<DesktopBootstrap | null>(null);
  const [bootstrapError, setBootstrapError] = useState<string | null>(null);
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [sessionsError, setSessionsError] = useState<string | null>(null);
  const [sessionKey, setSessionKey] = useState<string>(() => crypto.randomUUID());
  const [history, setHistory] = useState<SessionHistory | null>(null);
  const copy = VIEW_COPY[view];

  useEffect(() => {
    let active = true;
    void window.miniclaw.bootstrap().then((value) => {
      if (active) {
        setBootstrap(value);
        setBootstrapError(null);
      }
    }).catch(() => {
      if (active) {
        setBootstrapError("无法连接 MiniClaw Core，请检查本地启动配置。");
      }
    });
    return () => {
      active = false;
    };
  }, []);

  useEffect(() => {
    if (!bootstrap || view !== "home") {
      return;
    }
    let active = true;
    void window.miniclaw.listSessions(20).then((value) => {
      if (active) {
        setSessions(value);
        setSessionsError(null);
      }
    }).catch(() => {
      if (active) {
        setSessionsError("最近任务读取失败，请稍后重试。");
      }
    });
    return () => {
      active = false;
    };
  }, [bootstrap, view]);

  function createTask(): void {
    setSessionKey(crypto.randomUUID());
    setHistory(null);
    setView("task");
  }

  async function openSession(selected: string): Promise<void> {
    setSessionsError(null);
    try {
      const loaded = await window.miniclaw.loadSession(selected, 100);
      setSessionKey(selected);
      setHistory(loaded);
      setView("task");
    } catch {
      setSessionsError("任务历史读取失败，请稍后重试。");
    }
  }

  return (
    <div className="app-shell min-h-screen">
      <aside className="sidebar">
        <div className="brand" aria-label="MiniClaw Desktop">
          <span className="brand-mark" aria-hidden="true">M</span>
          <span>MiniClaw</span>
        </div>
        <nav className="navigation" aria-label="主导航">
          {NAV_ITEMS.map((item) => (
            <button
              className="nav-item"
              data-active={view === item.id}
              key={item.id}
              onClick={() => setView(item.id)}
              type="button"
            >
              <span className="nav-mark" aria-hidden="true">{item.mark}</span>
              <span>{item.label}</span>
            </button>
          ))}
        </nav>
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
            onSelectSession={(selected) => void openSession(selected)}
            sessionKey={sessionKey}
            sessions={sessions}
          />
        ) : (
          <>
            <section className="intro" aria-labelledby={`${view}-title`}>
              <h1 id={`${view}-title`}>{copy.title}</h1>
              <p>{copy.body}</p>
            </section>
            <ViewPreview
              bootstrap={bootstrap}
              onCreateTask={createTask}
              onOpenSession={(selected) => void openSession(selected)}
              sessions={sessions}
              sessionsError={sessionsError}
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
  sessions,
  sessionsError,
  onCreateTask,
  onOpenSession,
}: {
  view: Exclude<ViewId, "task">;
  bootstrap: DesktopBootstrap | null;
  sessions: SessionSummary[];
  sessionsError: string | null;
  onCreateTask: () => void;
  onOpenSession: (sessionKey: string) => void;
}): React.JSX.Element {
  if (view === "home") {
    return (
      <div className="home-grid">
        <section className="draft-card" aria-label="新任务入口">
          <span>新任务</span>
          <p>创建一个独立任务，在工作台里描述目标、查看过程并处理审批。</p>
          <button
            className="button-primary home-create-button"
            disabled={bootstrap === null}
            onClick={onCreateTask}
            type="button"
          >
            新建任务
          </button>
        </section>
        <section className="recent-card" aria-label="最近任务">
          <div className="recent-heading">
            <span>最近任务</span>
            <strong>{sessions.length}</strong>
          </div>
          {sessionsError ? <p className="recent-error" role="alert">{sessionsError}</p> : null}
          {sessions.length === 0 && !sessionsError ? (
            <p className="recent-empty">还没有任务，先创建一个。</p>
          ) : (
            <div className="recent-list">
              {sessions.map((session) => (
                <button
                  key={session.sessionKey}
                  onClick={() => onOpenSession(session.sessionKey)}
                  type="button"
                >
                  <span>
                    <strong>{session.title}</strong>
                    <small>{session.status}</small>
                  </span>
                  <time>{new Date(session.updatedAt).toLocaleDateString("zh-CN")}</time>
                </button>
              ))}
            </div>
          )}
        </section>
      </div>
    );
  }
  return (
    <section className="empty-panel">
      <span className="empty-index">{view === "automation" ? "A" : "S"}</span>
      <p>{view === "automation"
        ? "W1 下一步读取已有自动化"
        : bootstrap
          ? `${bootstrap.model} · ${bootstrap.workspace}`
          : "Core 接通后显示本地运行设置"}</p>
    </section>
  );
}
