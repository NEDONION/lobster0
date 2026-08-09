import type { Metadata } from 'next';
import { notFound } from 'next/navigation';

import { MarketingHome } from '@/components/marketing/MarketingHome';
import { marketingCopy } from '@/content/site';
import { getLocale, localizedPath } from '@/lib/i18n';

const siteUrl = 'https://miniclaw.vercel.app';

export async function generateMetadata({
  params,
}: {
  params: Promise<{ lang: string }>;
}): Promise<Metadata> {
  const locale = getLocale((await params).lang);
  if (!locale) return {};
  const copy = marketingCopy[locale];
  const canonicalPath = localizedPath(locale, '/');

  return {
    alternates: {
      canonical: `${siteUrl}${canonicalPath}`,
      languages: {
        'zh-CN': `${siteUrl}/`,
        en: `${siteUrl}/en`,
      },
    },
    description: copy.meta.description,
    openGraph: { url: `${siteUrl}${canonicalPath}` },
    title: { absolute: copy.meta.title },
  };
}

export default async function MarketingPage({ params }: { params: Promise<{ lang: string }> }) {
  const locale = getLocale((await params).lang);
  if (locale === null) notFound();

  return <MarketingHome locale={locale} />;
}
