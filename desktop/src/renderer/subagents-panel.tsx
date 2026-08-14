import type { SubagentSummary } from "../common/api";

interface SubagentsPanelProps {
  subagents: SubagentSummary[];
  error: string | null;
}

export function SubagentsPanel({ subagents, error }: SubagentsPanelProps): React.JSX.Element {
  return (
    <section className="subagents-panel" aria-label="子 Agent">
      <header className="subagents-header">
        <h2>子 Agent</h2>
        {/* 说清「谁来决定派发」，避免被误解成这里可以选用哪个 Agent 回答。 */}
        <p className="subagents-note">
          由主 Agent 在后台任务中自行调用，子 Agent 不能再派发。
        </p>
      </header>

      {error ? <p className="panel-error" role="alert">{error}</p> : null}

      {subagents.length === 0 ? (
        // 空白无法区分「没配」与「功能坏了」，空态必须自己说明自己。
        <div className="subagents-empty">
          <p>尚未声明子 Agent，主 Agent 不会派发子任务。</p>
          <p className="subagents-note">
            两步都要做才生效：在配置里写 <code>[[subagents]]</code>，
            并把 <code>delegate_task</code> 加进 <code>tools.enabled</code>。
          </p>
        </div>
      ) : (
        <ul className="subagents-list">
          {subagents.map((item) => (
            <li className="subagents-card" key={item.id}>
              <div className="subagents-card-head">
                <span className="subagents-card-id">{item.id}</span>
              </div>
              <p className="subagents-card-description">{item.description}</p>
              {/* 只显示预算，不显示工具集：那是安全边界，界面既改不了也不该复述。 */}
              <p className="subagents-card-budget">
                最多 {item.maxTurns} 轮 · {item.maxToolCalls} 次工具调用 ·{" "}
                {item.timeoutSeconds} 秒
              </p>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
