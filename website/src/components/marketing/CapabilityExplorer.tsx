import { marketingCopy, type CapabilityCopy, type Locale } from '@/content/site';

import { AutomationPanel } from './capabilities/AutomationPanel';
import { ChannelsPanel } from './capabilities/ChannelsPanel';
import { MemoryPanel } from './capabilities/MemoryPanel';
import { RuntimePanel } from './capabilities/RuntimePanel';
import { SafetyPanel } from './capabilities/SafetyPanel';
import { HashTabs, type HashTabItem } from './HashTabs';

function renderCapabilityPanel(capability: CapabilityCopy) {
  switch (capability.id) {
    case 'runtime':
      return <RuntimePanel copy={capability} />;
    case 'channels':
      return <ChannelsPanel copy={capability} />;
    case 'safety':
      return <SafetyPanel copy={capability} />;
    case 'memory':
      return <MemoryPanel copy={capability} />;
    case 'automation':
      return <AutomationPanel copy={capability} />;
  }
}

export function CapabilityExplorer({ locale }: { locale: Locale }) {
  const copy = marketingCopy[locale];
  const items: HashTabItem[] = copy.capabilities.map((capability) => ({
    id: capability.id,
    label: capability.label,
    panel: renderCapabilityPanel(capability),
  }));

  return (
    <section className="marketing-section capability-explorer" id="product" aria-labelledby="product-title">
      <div className="site-shell section-frame">
        <div className="section-heading-grid">
          <div>
            <p className="section-kicker">{copy.product.eyebrow}</p>
            <h2 id="product-title">{copy.product.title}</h2>
          </div>
          <p className="section-lead">{copy.product.lead}</p>
        </div>
        <HashTabs ariaLabel={copy.nav.product} items={items} />
      </div>
    </section>
  );
}
