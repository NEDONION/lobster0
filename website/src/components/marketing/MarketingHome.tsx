import { marketingCopy, type Locale } from '@/content/site';

import { CapabilityExplorer } from './CapabilityExplorer';
import { HeroRuntime } from './HeroRuntime';
import { MarketingFooter } from './MarketingFooter';
import { MarketingHeader } from './MarketingHeader';

export function MarketingHome({ locale }: { locale: Locale }) {
  const copy = marketingCopy[locale];

  return (
    <div className="marketing-page">
      <MarketingHeader locale={locale} />
      <main>
        <HeroRuntime locale={locale} />

        <CapabilityExplorer locale={locale} />

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
