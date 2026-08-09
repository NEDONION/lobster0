import { useEffect, useState } from "react";

import type { DesktopBootstrap } from "../common/api";
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
            key="desktop-default"
            sessionKey="desktop-default"
          />
        ) : (
          <>
            <section className="intro" aria-labelledby={`${view}-title`}>
              <h1 id={`${view}-title`}>{copy.title}</h1>
              <p>{copy.body}</p>
            </section>
            <ViewPreview view={view} bootstrap={bootstrap} />
          </>
        )}
      </main>
    </div>
  );
}

function ViewPreview({
  view,
  bootstrap,
}: {
  view: Exclude<ViewId, "task">;
  bootstrap: DesktopBootstrap | null;
}): React.JSX.Element {
  if (view === "home") {
    return (
      <section className="draft-card" aria-label="新任务入口预览">
        <span>新任务</span>
        <p>接通 Core 后，从这里输入目标并选择工作目录。</p>
        <div className="draft-line" aria-hidden="true" />
      </section>
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
