import { notFound } from 'next/navigation';

import { MarketingHome } from '@/components/marketing/MarketingHome';
import { getLocale } from '@/lib/i18n';

export default async function MarketingPage({ params }: { params: Promise<{ lang: string }> }) {
  const locale = getLocale((await params).lang);
  if (locale === null) notFound();

  return <MarketingHome locale={locale} />;
}
