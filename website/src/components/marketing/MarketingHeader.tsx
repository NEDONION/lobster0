import Link from 'next/link';

import { marketingCopy, siteFacts, type Locale } from '@/content/site';
import { localizedPath } from '@/lib/i18n';

import { BrandMark } from './BrandMark';

export function MarketingHeader({ locale }: { locale: Locale }) {
  const copy = marketingCopy[locale];
  const alternateLocale: Locale = locale === 'zh-CN' ? 'en' : 'zh-CN';

  return (
    <header className="marketing-header">
      <div className="site-shell marketing-header__inner">
        <Link className="brand" href={localizedPath(locale, '/')} aria-label="MiniClaw home">
          <BrandMark className="brand__mark" size={36} />
          <span>MiniClaw</span>
        </Link>
        <nav className="marketing-nav" aria-label={locale === 'zh-CN' ? '主导航' : 'Primary'}>
          <a href="#product">{copy.nav.product}</a>
          <a href="#workbench">{copy.nav.workbench}</a>
          <Link href={localizedPath(locale, '/docs')}>{copy.nav.docs}</Link>
        </nav>
        <div className="marketing-header__actions">
          <Link className="language-link" href={localizedPath(alternateLocale, '/')}>
            {copy.nav.language}
          </Link>
          <a className="header-github" href={siteFacts.links.github}>
            {copy.nav.github}
            <span aria-hidden="true">↗</span>
          </a>
        </div>
      </div>
    </header>
  );
}
