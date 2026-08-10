import { siteFacts, type CapabilityCopy, type Locale } from '@/content/site';

export function SafetyPanel({ copy, locale }: { copy: CapabilityCopy; locale: Locale }) {
  const zh = locale === 'zh-CN';

  return (
    <article className="capability-panel capability-panel--safety">
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
      <div className="safety-map" aria-label={zh ? 'Lobster0 权限模式与安全边界' : 'Lobster0 permission modes and boundaries'}>
        <div className="safety-map__modes">
          {siteFacts.permissionModes.map((mode, index) => (
            <div data-active={index === 0 ? 'true' : undefined} key={mode}>
              <span>{String(index + 1).padStart(2, '0')}</span>
              <strong>{mode}</strong>
            </div>
          ))}
        </div>
        <div className="safety-map__readout">
          <span>{zh ? '工作区' : 'WORKSPACE'}</span><strong>{zh ? '所有者范围' : 'owner-scoped'}</strong>
          <span>{zh ? '命令' : 'COMMAND'}</span><strong>{zh ? '程序 + exact argv[]' : 'program + exact argv[]'}</strong>
          <span>{zh ? '网络' : 'NETWORK'}</span><strong>{zh ? 'SSRF 防护' : 'SSRF guarded'}</strong>
          <span>{zh ? '密钥' : 'SECRET'}</span><strong>{zh ? '脱敏 / 永不提交' : 'redacted / never committed'}</strong>
        </div>
      </div>
    </article>
  );
}
