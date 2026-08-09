import type { CapabilityCopy } from '@/content/site';

export function MemoryPanel({ copy }: { copy: CapabilityCopy }) {
  return (
    <article className="capability-panel capability-panel--memory">
      <div className="capability-panel__copy">
        <span>04 / OWNER BOUNDARY</span>
        <h3>{copy.title}</h3>
        <p>{copy.summary}</p>
        <ul className="capability-facts">
          {copy.facts.map((fact) => (
            <li key={fact}>{fact}</li>
          ))}
        </ul>
      </div>
      <div className="memory-map" aria-label="Markdown truth and SQLite projection">
        <span className="memory-map__owner">OWNER BOUNDARY / LOCAL WORKSPACE</span>
        <div>
          <span>TRUTH</span>
          <strong>Markdown</strong>
          <small>readable · reviewable · portable</small>
        </div>
        <i aria-hidden="true">→ projection →</i>
        <div>
          <span>CONTROL PLANE</span>
          <strong>SQLite</strong>
          <small>structured · indexed · rebuildable</small>
        </div>
      </div>
    </article>
  );
}
