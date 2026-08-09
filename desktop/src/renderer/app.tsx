import { useState } from "react";

import { NAV_ITEMS, type ViewId } from "./navigation";

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
  const copy = VIEW_COPY[view];

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
          <span className="pulse-dot" aria-hidden="true" />
          <span>Desktop shell</span>
          <strong>W0</strong>
        </div>
      </aside>

      <main className="workspace" data-view={view}>
        <header className="workspace-header">
          <span className="eyebrow">{copy.eyebrow}</span>
          <span className="local-badge">本地优先</span>
        </header>
        <section className="intro" aria-labelledby={`${view}-title`}>
          <h1 id={`${view}-title`}>{copy.title}</h1>
          <p>{copy.body}</p>
        </section>
        <ViewPreview view={view} />
      </main>
    </div>
  );
}

function ViewPreview({ view }: { view: ViewId }): React.JSX.Element {
  if (view === "home") {
    return (
      <section className="draft-card" aria-label="新任务入口预览">
        <span>新任务</span>
        <p>接通 Core 后，从这里输入目标并选择工作目录。</p>
        <div className="draft-line" aria-hidden="true" />
      </section>
    );
  }
  if (view === "task") {
    return (
      <section className="workbench-preview" aria-label="任务工作台布局预览">
        <div><span>任务</span><strong>当前任务</strong></div>
        <div><span>过程</span><strong>等待开始</strong></div>
        <div><span>结果</span><strong>尚无产物</strong></div>
      </section>
    );
  }
  return (
    <section className="empty-panel">
      <span className="empty-index">{view === "automation" ? "A" : "S"}</span>
      <p>{view === "automation" ? "Core 接通后读取已有自动化" : "Core 接通后显示本地运行设置"}</p>
    </section>
  );
}
