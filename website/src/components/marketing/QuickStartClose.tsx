import Link from 'next/link';

import { marketingCopy, siteFacts, type Locale } from '@/content/site';
import { localizedPath } from '@/lib/i18n';

import { CommandCopy } from './CommandCopy';

export function QuickStartClose({ locale }: { locale: Locale }) {
  const copy = marketingCopy[locale];
  const ui = copy.ui;

  return (
    <div className="quick-start-close">
      <div className="quick-start-close__copy">
        <p className="section-kicker">{copy.quickStart.eyebrow}</p>
        <h3>{copy.quickStart.title}</h3>
        <p>{copy.quickStart.lead}</p>
        <div className="quick-start-close__requirements" aria-label={ui.requirements}>
          <span>Python {siteFacts.requirements.python}</span>
          <span>{siteFacts.requirements.installer}</span>
          <span>v{siteFacts.version}</span>
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
      {/* Two blocks, not one: `lobster0 setup` is interactive, so the start
          commands must never end up in the same paste as the installer. */}
      <div className="quick-start-close__commands">
        <CommandCopy
          command={siteFacts.install}
          copiedLabel={copy.hero.copiedLabel}
          label={copy.hero.copyLabel}
          title={copy.hero.installLabel}
        />
        <CommandCopy
          command={siteFacts.start}
          copiedLabel={copy.hero.copiedLabel}
          label={copy.hero.copyLabel}
          title={copy.hero.startLabel}
        />
      </div>
    </div>
  );
}
