import Link from 'next/link';

import { marketingCopy, siteFacts, type Locale } from '@/content/site';
import { localizedPath } from '@/lib/i18n';

import { ClawTrace } from './ClawTrace';
import { CommandCopy } from './CommandCopy';
import { EvidenceStrip } from './EvidenceStrip';

export function HeroRuntime({ locale }: { locale: Locale }) {
  const copy = marketingCopy[locale];

  return (
    <section className="marketing-section hero-shell" id="hero" aria-labelledby="hero-title">
      <div className="site-shell hero-runtime">
        <div className="hero-runtime__copy">
          <p className="section-kicker">{copy.hero.eyebrow}</p>
          <h1 id="hero-title">{copy.hero.title}</h1>
          <p className="hero-runtime__lead">{copy.hero.lead}</p>
          <div className="hero-shell__actions">
            <Link className="button button--primary" href={localizedPath(locale, '/docs/getting-started')}>
              {copy.hero.primaryCta}
              <span aria-hidden="true">→</span>
            </Link>
            <a className="button button--secondary" href={siteFacts.links.github}>
              {copy.hero.secondaryCta}
              <span aria-hidden="true">↗</span>
            </a>
          </div>
          <CommandCopy
            command={siteFacts.install}
            copiedLabel={copy.hero.copiedLabel}
            label={copy.hero.copyLabel}
            title={copy.hero.installLabel}
          />
        </div>
        <ClawTrace copy={copy.trace} />
        <EvidenceStrip copy={copy.evidence} />
      </div>
    </section>
  );
}
