import { siteFacts, type CapabilityCopy } from '@/content/site';

export function SafetyPanel({ copy }: { copy: CapabilityCopy }) {
  return (
    <article className="capability-panel capability-panel--safety">
      <div className="capability-panel__copy">
        <span>03 / POLICY BEFORE ACTION</span>
        <h3>{copy.title}</h3>
        <p>{copy.summary}</p>
        <ul className="capability-facts">
          {copy.facts.map((fact) => (
            <li key={fact}>{fact}</li>
          ))}
        </ul>
      </div>
      <div className="safety-map" aria-label="MiniClaw permission modes and boundaries">
        <div className="safety-map__modes">
          {siteFacts.permissionModes.map((mode, index) => (
            <div data-active={index === 0 ? 'true' : undefined} key={mode}>
              <span>{String(index + 1).padStart(2, '0')}</span>
              <strong>{mode}</strong>
            </div>
          ))}
        </div>
        <div className="safety-map__readout">
          <span>WORKSPACE</span><strong>owner-scoped</strong>
          <span>COMMAND</span><strong>program + exact argv[]</strong>
          <span>NETWORK</span><strong>SSRF guarded</strong>
          <span>SECRET</span><strong>redacted / never committed</strong>
        </div>
      </div>
    </article>
  );
}
