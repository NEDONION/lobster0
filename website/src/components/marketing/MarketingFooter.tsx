import Link from 'next/link';

import { marketingCopy, siteFacts, type Locale } from '@/content/site';
import { localizedPath } from '@/lib/i18n';

export function MarketingFooter({ locale }: { locale: Locale }) {
  const copy = marketingCopy[locale];

  return (
    <footer className="marketing-footer">
      <div className="site-shell marketing-footer__inner">
        <p>{copy.footer.statement}</p>
        <nav aria-label={locale === 'zh-CN' ? '页脚导航' : 'Footer'}>
          <Link href={localizedPath(locale, '/docs')}>Lobster0 {copy.footer.docs}</Link>
          <a href={siteFacts.links.issues}>{copy.footer.issues}</a>
          <a href={siteFacts.links.github}>{copy.footer.source}</a>
        </nav>
      </div>
    </footer>
  );
}
