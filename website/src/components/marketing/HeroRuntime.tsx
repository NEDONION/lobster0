import Link from 'next/link';

import { marketingCopy, siteFacts, type Locale } from '@/content/site';
import { localizedPath } from '@/lib/i18n';

import { BrandMark } from './BrandMark';
import { ClawTrace } from './ClawTrace';
import { EvidenceStrip } from './EvidenceStrip';

export function HeroRuntime({ locale }: { locale: Locale }) {
  const copy = marketingCopy[locale];

  return (
    <section className="marketing-section hero-shell" id="hero" aria-labelledby="hero-title">
      <div className="site-shell hero-runtime">
        <div className="hero-runtime__copy">
          <BrandMark className="hero-runtime__mark" size={76} title="Lobster0" />
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
        </div>
        <ClawTrace copy={copy.trace} surfaces={copy.hero.surfaces} />
        <EvidenceStrip copy={copy.evidence} />
      </div>
    </section>
  );
}
