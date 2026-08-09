import { siteFacts, type CapabilityCopy, type Locale } from '@/content/site';

export function ChannelsPanel({ copy, locale }: { copy: CapabilityCopy; locale: Locale }) {
  const zh = locale === 'zh-CN';

  return (
    <article className="capability-panel capability-panel--channels">
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
      <div className="channels-map" aria-label={zh ? '一个 AgentRuntime 与四个隔离入口' : 'One AgentRuntime and four isolated surfaces'}>
        <div className="channels-map__core">
          <span>{zh ? '共享核心' : 'SHARED'}</span>
          <strong>AgentRuntime</strong>
          <small>{zh ? 'Python Core / 策略 / 记忆' : 'Python Core / Policy / Memory'}</small>
        </div>
        <div className="channels-map__edges">
          {siteFacts.surfaces.map((surface, index) => (
            <div key={surface}>
              <span>{zh ? '入口' : 'EDGE'} {String(index + 1).padStart(2, '0')}</span>
              <strong>{surface}</strong>
              <small>{zh ? '传输 · 交付 · 队列' : 'transport · delivery · queue'}</small>
            </div>
          ))}
        </div>
      </div>
    </article>
  );
}
