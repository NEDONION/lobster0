import Link from 'next/link';

import { marketingCopy, siteFacts, type Locale } from '@/content/site';
import { localizedPath } from '@/lib/i18n';

import { BrandMark } from './BrandMark';
import { LanguageSwitcher } from './LanguageSwitcher';

export function MarketingHeader({ locale }: { locale: Locale }) {
  const copy = marketingCopy[locale];
  const ui = copy.ui;

  return (
    <header className="marketing-header">
      <div className="site-shell marketing-header__inner">
        <Link className="brand" href={localizedPath(locale, '/')} aria-label="Lobster0 home">
          <BrandMark className="brand__mark" size={36} />
          <span>Lobster0</span>
        </Link>
        <nav className="marketing-nav" aria-label={ui.primaryNav}>
          <a href="#product">{copy.nav.product}</a>
          <a href="#workbench">{copy.nav.workbench}</a>
          <Link href={localizedPath(locale, '/docs')}>{copy.nav.docs}</Link>
        </nav>
        <div className="marketing-header__actions">
          <LanguageSwitcher label={copy.nav.language} locale={locale} />
          <a className="header-github" href={siteFacts.links.github}>
            {copy.nav.github}
            <span aria-hidden="true">↗</span>
          </a>
        </div>
      </div>
    </header>
  );
}
