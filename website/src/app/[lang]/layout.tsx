import '@fontsource-variable/instrument-sans';
import '@fontsource/ibm-plex-mono/400.css';
import 'fumadocs-ui/style.css';
import '@/styles/globals.css';

import { RootProvider } from 'fumadocs-ui/provider/next';
import { notFound } from 'next/navigation';
import type { ReactNode } from 'react';

import { getLocale, locales } from '@/lib/i18n';
import { i18nUI } from '@/lib/layout.shared';

export function generateStaticParams() {
  return locales.map((lang) => ({ lang }));
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
        <RootProvider
          i18n={i18nUI.provider(locale)}
          theme={{ defaultTheme: 'light', enableSystem: false, forcedTheme: 'light' }}
        >
          {children}
        </RootProvider>
      </body>
    </html>
  );
}
