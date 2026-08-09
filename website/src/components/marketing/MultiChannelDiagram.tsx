import { siteFacts, type Locale, type WorkflowCopy } from '@/content/site';

export function MultiChannelDiagram({ locale, workflow }: { locale: Locale; workflow: WorkflowCopy }) {
  const zh = locale === 'zh-CN';

  return (
    <article className="workflow-panel workflow-panel--channels">
      <div className="workflow-panel__copy">
        <span>03 / {zh ? '共享核心 · 隔离入口' : 'SHARED CORE · ISOLATED EDGES'}</span>
        <h3>{workflow.title}</h3>
        <p>{workflow.summary}</p>
        <dl>
          <div><dt>{zh ? '运行时' : 'RUNTIME'}</dt><dd>01 {zh ? '共享' : 'shared'}</dd></div>
          <div><dt>{zh ? '入口' : 'SURFACES'}</dt><dd>{String(siteFacts.counts.surfaces).padStart(2, '0')}</dd></div>
          <div><dt>{zh ? '场景' : 'CASES'}</dt><dd>{siteFacts.counts.channelCases}</dd></div>
        </dl>
      </div>
      <div className="multi-channel-map" aria-label={zh ? '共享 AgentRuntime 与隔离入口' : 'Shared AgentRuntime with isolated channel edges'}>
        <div className="multi-channel-map__runtime">
          <span>{zh ? '共享核心' : 'SHARED CORE'}</span>
          <strong>AgentRuntime</strong>
          <small>{zh ? 'Agent · 策略 · 工具 · 记忆' : 'Agent · Policy · Tools · Memory'}</small>
        </div>
        <div className="multi-channel-map__surfaces">
          {siteFacts.surfaces.map((surface, index) => (
            <div key={surface}>
              <header>
                <span>0{index + 1}</span>
                <strong>{surface}</strong>
                <i aria-label={zh ? '健康' : 'healthy'} />
              </header>
              <dl>
                <div><dt>{zh ? '传输' : 'Transport'}</dt><dd>{zh ? '隔离' : 'isolated'}</dd></div>
                <div><dt>{zh ? '交付' : 'Delivery'}</dt><dd>{zh ? '隔离' : 'isolated'}</dd></div>
                <div><dt>{zh ? '队列' : 'queue'}</dt><dd>{zh ? '隔离' : 'isolated'}</dd></div>
                <div><dt>{zh ? '故障' : 'failure'}</dt><dd>{zh ? '受控' : 'contained'}</dd></div>
              </dl>
            </div>
          ))}
        </div>
      </div>
    </article>
  );
}
