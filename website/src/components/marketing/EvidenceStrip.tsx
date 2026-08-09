import { siteFacts, type MarketingCopy } from '@/content/site';

export function EvidenceStrip({ copy }: { copy: MarketingCopy['evidence'] }) {
  const facts = [
    { label: copy.labels.core, value: '01' },
    { label: copy.labels.surfaces, value: String(siteFacts.counts.surfaces).padStart(2, '0') },
    { label: copy.labels.tools, value: String(siteFacts.counts.tools).padStart(2, '0') },
    { label: copy.labels.channelCases, value: String(siteFacts.counts.channelCases) },
    { label: copy.labels.automationCases, value: String(siteFacts.counts.automationCases) },
  ];

  return (
    <aside className="evidence-strip" aria-label="MiniClaw evidence">
      <dl>
        {facts.map((fact, index) => (
          <div data-accent={['blue', 'coral', 'green', 'amber', 'violet'][index]} key={fact.label}>
            <dt>{fact.label}</dt>
            <dd>{fact.value}</dd>
          </div>
        ))}
      </dl>
      <p>
        <span aria-hidden="true">!</span>
        {copy.disclosure}
      </p>
    </aside>
  );
}
