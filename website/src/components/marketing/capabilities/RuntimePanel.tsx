import { marketingCopy, type CapabilityCopy, type Locale } from '@/content/site';

export function RuntimePanel({ copy, locale }: { copy: CapabilityCopy; locale: Locale }) {
  const steps = marketingCopy[locale].trace.steps;

  return (
    <article className="capability-panel capability-panel--runtime">
      <div className="capability-panel__copy">
        <span>{copy.eyebrow}</span>
        <h3>{copy.title}</h3>
        <p>{copy.summary}</p>
        <ul className="capability-facts">
          {copy.facts.map((fact) => (
            <li key={fact}>{fact}</li>
          ))}
        </ul>
      </div>
      <ol className="runtime-map" aria-label={locale === 'zh-CN' ? 'MiniClaw 运行路径' : 'MiniClaw runtime path'}>
        {steps.map((step, index) => (
          <li key={step.event} data-state={step.state}>
            <span className="runtime-map__index">{String(index + 1).padStart(2, '0')}</span>
            <strong className="runtime-map__event">{step.event}</strong>
            <em className="runtime-map__state">{step.state}</em>
            <small className="runtime-map__detail">{step.detail}</small>
          </li>
        ))}
      </ol>
    </article>
  );
}
