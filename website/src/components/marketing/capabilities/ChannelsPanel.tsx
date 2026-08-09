import { siteFacts, type CapabilityCopy } from '@/content/site';

export function ChannelsPanel({ copy }: { copy: CapabilityCopy }) {
  return (
    <article className="capability-panel capability-panel--channels">
      <div className="capability-panel__copy">
        <span>02 / ISOLATED EDGES</span>
        <h3>{copy.title}</h3>
        <p>{copy.summary}</p>
        <ul className="capability-facts">
          {copy.facts.map((fact) => (
            <li key={fact}>{fact}</li>
          ))}
        </ul>
      </div>
      <div className="channels-map" aria-label="One AgentRuntime and four isolated surfaces">
        <div className="channels-map__core">
          <span>SHARED</span>
          <strong>AgentRuntime</strong>
          <small>Python Core / Policy / Memory</small>
        </div>
        <div className="channels-map__edges">
          {siteFacts.surfaces.map((surface, index) => (
            <div key={surface}>
              <span>EDGE {String(index + 1).padStart(2, '0')}</span>
              <strong>{surface}</strong>
              <small>transport · delivery · queue</small>
            </div>
          ))}
        </div>
      </div>
    </article>
  );
}
