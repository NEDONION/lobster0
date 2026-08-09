import { defineI18n } from 'fumadocs-core/i18n';

import type { Locale } from '@/content/site';

export const locales = ['zh-CN', 'en'] as const satisfies readonly Locale[];

export const i18n = defineI18n({
  languages: [...locales],
  defaultLanguage: 'zh-CN',
  fallbackLanguage: null,
  hideLocale: 'default-locale',
});

export function getLocale(value: string): Locale | null {
  return locales.find((locale) => locale === value) ?? null;
}

export function localizedPath(locale: Locale, path: `/${string}`): string {
  if (locale === 'zh-CN') return path;
  return path === '/' ? '/en' : `/en${path}`;
}

export function formatLocalePath(locale: string, pathname: string): string {
  return pathname === '/' ? `/${locale}` : `/${locale}${pathname}`;
}
