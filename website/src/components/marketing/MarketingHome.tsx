import { marketingCopy, siteFacts, type Locale } from '@/content/site';

import { MarketingFooter } from './MarketingFooter';
import { MarketingHeader } from './MarketingHeader';

export function MarketingHome({ locale }: { locale: Locale }) {
  const copy = marketingCopy[locale];

  return (
    <div className="marketing-page">
      <MarketingHeader locale={locale} />
      <main>
        <section className="marketing-section hero-shell" id="hero" aria-labelledby="hero-title">
          <div className="site-shell hero-shell__grid">
            <div className="hero-shell__copy">
              <p className="section-kicker">{copy.hero.eyebrow}</p>
              <h1 id="hero-title">{copy.hero.title}</h1>
              <p>{copy.hero.lead}</p>
              <div className="hero-shell__actions">
                <a className="button button--primary" href={locale === 'zh-CN' ? '/docs/getting-started' : '/en/docs/getting-started'}>
                  {copy.hero.primaryCta}
                </a>
                <a className="button button--secondary" href={siteFacts.links.github}>
                  {copy.hero.secondaryCta}
                </a>
              </div>
            </div>
            <div className="instrument-placeholder" aria-label={copy.trace.title}>
              <span>{copy.trace.eyebrow}</span>
              <strong>{copy.trace.title}</strong>
              <p>{copy.trace.description}</p>
            </div>
          </div>
        </section>

        <section className="marketing-section" id="product" aria-labelledby="product-title">
          <div className="site-shell section-frame">
            <p className="section-kicker">{copy.product.eyebrow}</p>
            <h2 id="product-title">{copy.product.title}</h2>
            <p className="section-lead">{copy.product.lead}</p>
            <div className="shell-preview" aria-label={copy.nav.product}>
              {copy.capabilities.map((capability) => (
                <span key={capability.id}>{capability.label}</span>
              ))}
            </div>
          </div>
        </section>

        <section className="marketing-section" id="workbench" aria-labelledby="workbench-title">
          <div className="site-shell section-frame">
            <p className="section-kicker">{copy.workbench.eyebrow}</p>
            <h2 id="workbench-title">{copy.workbench.title}</h2>
            <p className="section-lead">{copy.workbench.lead}</p>
            <div className="shell-preview" aria-label={copy.nav.workbench}>
              {copy.workflows.map((workflow) => (
                <span key={workflow.id}>{workflow.label}</span>
              ))}
            </div>
          </div>
        </section>
      </main>
      <MarketingFooter locale={locale} />
    </div>
  );
}
