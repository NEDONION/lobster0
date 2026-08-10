import '@fontsource-variable/instrument-sans';
import '@fontsource/ibm-plex-mono/400.css';
import 'fumadocs-ui/style.css';
import '@/styles/globals.css';

import type { Metadata } from 'next';
import { notFound } from 'next/navigation';
import type { ReactNode } from 'react';

import { marketingCopy } from '@/content/site';
import { AppProvider } from '@/components/AppProvider';
import { getLocale, locales } from '@/lib/i18n';
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
    metadataBase: new URL('https://lobster0.vercel.app'),
    openGraph: {
      description: copy.meta.description,
      images: [
        {
          alt: copy.meta.title,
          height: 630,
          url: 'https://lobster0.vercel.app/opengraph-image',
          width: 1200,
        },
      ],
      locale: locale === 'zh-CN' ? 'zh_CN' : 'en_US',
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
      images: ['https://lobster0.vercel.app/opengraph-image'],
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
    <html lang={locale} suppressHydrationWarning>
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
