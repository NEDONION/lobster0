import '@/styles/globals.css';

import type { Metadata } from 'next';
import { notFound } from 'next/navigation';
import type { ReactNode } from 'react';

import { marketingCopy, siteFacts } from '@/content/site';
import { AppProvider } from '@/components/AppProvider';
import { getLocale, openGraphLocales, locales } from '@/lib/i18n';
import { instrumentSans, plexMono } from '@/lib/fonts';
import { i18nUI } from '@/lib/layout.shared';

export function generateStaticParams() {
  return locales.map((lang) => ({ lang }));
}

export async function generateMetadata({
  params,
}: {
  params: Promise<{ lang: string }>;
}): Promise<Metadata> {
  const locale = getLocale((await params).lang);
  if (!locale) return {};
  const copy = marketingCopy[locale];

  return {
    description: copy.meta.description,
    icons: { icon: '/favicon.svg' },
    metadataBase: new URL(siteFacts.siteUrl),
    openGraph: {
      description: copy.meta.description,
      images: [
        {
          alt: copy.meta.title,
          height: 630,
          url: `${siteFacts.siteUrl}/opengraph-image`,
          width: 1200,
        },
      ],
      locale: openGraphLocales[locale],
      siteName: 'Lobster0',
      title: copy.meta.title,
      type: 'website',
    },
    title: {
      default: copy.meta.title,
      template: '%s — Lobster0',
    },
    twitter: {
      card: 'summary_large_image',
      description: copy.meta.description,
      images: [`${siteFacts.siteUrl}/opengraph-image`],
      title: copy.meta.title,
    },
  };
}

export default async function LocaleLayout({
  children,
  params,
}: Readonly<{
  children: ReactNode;
  params: Promise<{ lang: string }>;
}>) {
  const { lang } = await params;
  const locale = getLocale(lang);
  if (!locale) notFound();

  return (
    <html className={`${instrumentSans.variable} ${plexMono.variable}`} lang={locale} suppressHydrationWarning>
      <body>
        <AppProvider
          i18n={i18nUI.provider(locale)}
          theme={{ defaultTheme: 'light', enableSystem: false, forcedTheme: 'light' }}
        >
          {children}
        </AppProvider>
      </body>
    </html>
  );
}
