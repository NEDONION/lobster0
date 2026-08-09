import { marketingCopy, type Locale } from '@/content/site';

import { CapabilityExplorer } from './CapabilityExplorer';
import { HeroRuntime } from './HeroRuntime';
import { MarketingFooter } from './MarketingFooter';
import { MarketingHeader } from './MarketingHeader';
import { Workbench } from './Workbench';

export function MarketingHome({ locale }: { locale: Locale }) {
  const copy = marketingCopy[locale];

  return (
    <div className="marketing-page">
      <MarketingHeader locale={locale} />
      <main>
        <HeroRuntime locale={locale} />

        <CapabilityExplorer locale={locale} />

        <Workbench locale={locale} workflows={copy.workflows} />
      </main>
      <MarketingFooter locale={locale} />
    </div>
  );
}
