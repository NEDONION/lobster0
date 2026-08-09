import { siteFacts, type WorkflowCopy } from '@/content/site';

export function MultiChannelDiagram({ workflow }: { workflow: WorkflowCopy }) {
  return (
    <article className="workflow-panel workflow-panel--channels">
      <div className="workflow-panel__copy">
        <span>03 / SHARED CORE · ISOLATED EDGES</span>
        <h3>{workflow.title}</h3>
        <p>{workflow.summary}</p>
        <dl>
          <div><dt>RUNTIME</dt><dd>01 shared</dd></div>
          <div><dt>SURFACES</dt><dd>{String(siteFacts.counts.surfaces).padStart(2, '0')}</dd></div>
          <div><dt>CASES</dt><dd>{siteFacts.counts.channelCases}</dd></div>
        </dl>
      </div>
      <div className="multi-channel-map" aria-label="Shared AgentRuntime with isolated channel edges">
        <div className="multi-channel-map__runtime">
          <span>SHARED CORE</span>
          <strong>AgentRuntime</strong>
          <small>Agent · Policy · Tools · Memory</small>
        </div>
        <div className="multi-channel-map__surfaces">
          {siteFacts.surfaces.map((surface, index) => (
            <div key={surface}>
              <header>
                <span>0{index + 1}</span>
                <strong>{surface}</strong>
                <i aria-label="healthy" />
              </header>
              <dl>
                <div><dt>Transport</dt><dd>isolated</dd></div>
                <div><dt>Delivery</dt><dd>isolated</dd></div>
                <div><dt>queue</dt><dd>isolated</dd></div>
                <div><dt>failure</dt><dd>contained</dd></div>
              </dl>
            </div>
          ))}
        </div>
      </div>
    </article>
  );
}
