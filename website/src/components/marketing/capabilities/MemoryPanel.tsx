import type { CapabilityCopy, Locale } from '@/content/site';

export function MemoryPanel({ copy, locale }: { copy: CapabilityCopy; locale: Locale }) {
  const zh = locale === 'zh-CN';

  return (
    <article className="capability-panel capability-panel--memory">
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
      <div className="memory-map" aria-label={zh ? 'Markdown 事实源与 SQLite 投影' : 'Markdown truth and SQLite projection'}>
        <span className="memory-map__owner">{zh ? '所有者边界 / 本地工作区' : 'OWNER BOUNDARY / LOCAL WORKSPACE'}</span>
        <div>
          <span>{zh ? '事实源' : 'TRUTH'}</span>
          <strong>Markdown</strong>
          <small>{zh ? '可读 · 可审查 · 可迁移' : 'readable · reviewable · portable'}</small>
        </div>
        <i aria-hidden="true">→ {zh ? '投影' : 'projection'} →</i>
        <div>
          <span>{zh ? '控制面' : 'CONTROL PLANE'}</span>
          <strong>SQLite</strong>
          <small>{zh ? '结构化 · 可索引 · 可重建' : 'structured · indexed · rebuildable'}</small>
        </div>
      </div>
    </article>
  );
}
