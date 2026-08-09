import { siteFacts, type CapabilityCopy } from '@/content/site';

export function AutomationPanel({ copy }: { copy: CapabilityCopy }) {
  return (
    <article className="capability-panel capability-panel--automation">
      <div className="capability-panel__copy">
        <span>05 / EXPLICIT GATE</span>
        <h3>{copy.title}</h3>
        <p>{copy.summary}</p>
        <ul className="capability-facts">
          {copy.facts.map((fact) => (
            <li key={fact}>{fact}</li>
          ))}
        </ul>
      </div>
      <div className="automation-map" aria-label="Disabled by default automation gate">
        <div className="automation-map__status">
          <span>DEFAULT STATE</span>
          <strong>{siteFacts.status.automationDefault ? 'ENABLED' : 'DISABLED'}</strong>
        </div>
        <div className="automation-map__flow">
          <span>OWNER ENABLES</span><i aria-hidden="true">→</i>
          <span>POLICY GATE</span><i aria-hidden="true">→</i>
          <span>{siteFacts.counts.automationCases} VERSIONED CASES</span>
        </div>
        <p>IMPLEMENTATION PASS <strong>≠</strong> LIVE PASS</p>
      </div>
    </article>
  );
}
