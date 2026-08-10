import { siteFacts, type CapabilityCopy, type Locale } from '@/content/site';

export function AutomationPanel({ copy, locale }: { copy: CapabilityCopy; locale: Locale }) {
  const zh = locale === 'zh-CN';

  return (
    <article className="capability-panel capability-panel--automation">
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
      <div className="automation-map" aria-label={zh ? '默认关闭的自动化门禁' : 'Disabled by default automation gate'}>
        <div className="automation-map__status">
          <span>{zh ? '默认状态' : 'DEFAULT STATE'}</span>
          <strong>{siteFacts.status.automationDefault ? (zh ? '开启' : 'ENABLED') : (zh ? '关闭' : 'DISABLED')}</strong>
        </div>
        <div className="automation-map__flow">
          <span>{zh ? '所有者启用' : 'OWNER ENABLES'}</span><i aria-hidden="true">→</i>
          <span>{zh ? '策略门禁' : 'POLICY GATE'}</span><i aria-hidden="true">→</i>
          <span>{siteFacts.counts.automationCases} {zh ? '条版本化场景' : 'VERSIONED CASES'}</span>
        </div>
        <p>{zh ? '实现通过' : 'IMPLEMENTATION PASS'} <strong>≠</strong> {zh ? '真实环境通过' : 'LIVE PASS'}</p>
      </div>
    </article>
  );
}
