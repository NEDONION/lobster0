import Link from 'next/link';

import { marketingCopy, siteFacts, type Locale } from '@/content/site';
import { localizedPath } from '@/lib/i18n';

import { CommandCopy } from './CommandCopy';

export function QuickStartClose({ locale }: { locale: Locale }) {
  const copy = marketingCopy[locale];

  return (
    <div className="quick-start-close">
      <div className="quick-start-close__copy">
        <p className="section-kicker">{copy.quickStart.eyebrow}</p>
        <h3>{copy.quickStart.title}</h3>
        <p>{copy.quickStart.lead}</p>
        <div className="quick-start-close__requirements" aria-label="Requirements">
          <span>Python {siteFacts.requirements.python}</span>
          <span>Node.js {siteFacts.requirements.node}</span>
        </div>
        <div className="quick-start-close__actions">
          <Link className="button button--primary" href={localizedPath(locale, '/docs/getting-started')}>
            {copy.quickStart.docsCta}<span aria-hidden="true">→</span>
          </Link>
          <a className="button button--secondary" href={siteFacts.links.github}>
            {copy.quickStart.githubCta}<span aria-hidden="true">↗</span>
          </a>
        </div>
      </div>
      <CommandCopy
        command={siteFacts.install}
        copiedLabel={copy.hero.copiedLabel}
        label={copy.hero.copyLabel}
        title={copy.hero.installLabel}
      />
    </div>
  );
}
