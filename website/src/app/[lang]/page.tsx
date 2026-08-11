import type { Metadata } from 'next';
import { notFound } from 'next/navigation';

import { MarketingHome } from '@/components/marketing/MarketingHome';
import { marketingCopy, siteFacts } from '@/content/site';
import { getLocale, languageAlternates, localizedPath, openGraphLocales } from '@/lib/i18n';

const siteUrl = siteFacts.siteUrl;

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
      languages: languageAlternates(siteUrl, '/'),
    },
    description: copy.meta.description,
    metadataBase: new URL(siteUrl),
    openGraph: {
      // Next merges `openGraph` field-by-field, not deeply: anything the layout
      // sets but this object omits is dropped, so locale/siteName/type must be
      // repeated here or the page ships without them.
      images: [{ alt: copy.meta.title, height: 630, url: `${siteUrl}/opengraph-image`, width: 1200 }],
      locale: openGraphLocales[locale],
      siteName: 'Lobster0',
      type: 'website',
      url: `${siteUrl}${canonicalPath}`,
    },
    title: { absolute: copy.meta.title },
  };
}

export default async function MarketingPage({ params }: { params: Promise<{ lang: string }> }) {
  const locale = getLocale((await params).lang);
  if (locale === null) notFound();

  return <MarketingHome locale={locale} />;
}
