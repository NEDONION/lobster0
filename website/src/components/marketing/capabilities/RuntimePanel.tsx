import { siteFacts, type CapabilityCopy } from '@/content/site';

export function RuntimePanel({ copy }: { copy: CapabilityCopy }) {
  return (
    <article className="capability-panel capability-panel--runtime">
      <div className="capability-panel__copy">
        <span>01 / CORE LOOP</span>
        <h3>{copy.title}</h3>
        <p>{copy.summary}</p>
        <ul className="capability-facts">
          {copy.facts.map((fact) => (
            <li key={fact}>{fact}</li>
          ))}
        </ul>
      </div>
      <div className="runtime-map" aria-label="MiniClaw runtime path">
        {siteFacts.traceEvents.map((event, index) => (
          <div key={event}>
            <span>{String(index + 1).padStart(2, '0')}</span>
            <strong>{event}</strong>
            {index < siteFacts.traceEvents.length - 1 ? <i aria-hidden="true">→</i> : null}
          </div>
        ))}
      </div>
    </article>
  );
}
