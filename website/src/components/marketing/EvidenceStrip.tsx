import { siteFacts, type MarketingCopy } from '@/content/site';

export function EvidenceStrip({ copy }: { copy: MarketingCopy['evidence'] }) {
  const facts = [
    { label: copy.labels.core, value: '1' },
    { label: copy.labels.surfaces, value: String(siteFacts.counts.surfaces) },
    { label: copy.labels.tools, value: String(siteFacts.counts.tools) },
    { label: copy.labels.permissionModes, value: String(siteFacts.counts.permissionModes) },
    { label: copy.labels.dataLocation, value: copy.labels.dataLocationValue },
  ];

  return (
    <aside className="evidence-strip" aria-label="Lobster0 evidence">
      <dl>
        {facts.map((fact) => (
          <div key={fact.label}>
            <dd>{fact.value}</dd>
            <dt>{fact.label}</dt>
          </div>
        ))}
      </dl>
    </aside>
  );
}
