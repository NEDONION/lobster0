import { defineI18n } from 'fumadocs-core/i18n';

import type { Locale } from '@/content/site';

export const locales = ['zh-CN', 'en', 'ja', 'ko', 'fr'] as const satisfies readonly Locale[];

export const localeNames: Record<Locale, string> = {
  'zh-CN': '简体中文',
  en: 'English',
  ja: '日本語',
  ko: '한국어',
  fr: 'Français',
};

/** Short label shown inside the compact language toggle. */
export const localeShortNames: Record<Locale, string> = {
  'zh-CN': '中',
  en: 'EN',
  ja: 'JA',
  ko: 'KO',
  fr: 'FR',
};

/** OpenGraph `og:locale` value per language. */
export const openGraphLocales: Record<Locale, string> = {
  'zh-CN': 'zh_CN',
  en: 'en_US',
  ja: 'ja_JP',
  ko: 'ko_KR',
  fr: 'fr_FR',
};

const i18nBypassPaths = new Set([
  '/favicon.svg',
  '/opengraph-image',
  '/robots.txt',
  '/sitemap.xml',
]);

const i18nBypassPrefixes = ['/api', '/_next', '/images'];

export const i18n = defineI18n({
  languages: [...locales],
  defaultLanguage: 'zh-CN',
  // Docs are only authored in zh-CN and en. ja/ko/fr are marketing-only locales,
  // so their /docs routes fall back to English instead of 404-ing.
  fallbackLanguage: 'en',
  hideLocale: 'default-locale',
});

export function getLocale(value: string): Locale | null {
  return locales.find((locale) => locale === value) ?? null;
}

export function isI18nBypassPath(pathname: string): boolean {
  if (i18nBypassPaths.has(pathname)) return true;
  return i18nBypassPrefixes.some(
    (prefix) => pathname === prefix || pathname.startsWith(`${prefix}/`),
  );
}

export function localizedPath(locale: Locale, path: `/${string}`): string {
  if (locale === 'zh-CN') return path;
  return path === '/' ? `/${locale}` : `/${locale}${path}`;
}

/**
 * hreflang map for one logical page, covering every supported locale.
 * Keep page metadata using this instead of hand-listing locales, so adding a
 * language can never silently leave stale two-language alternates behind.
 */
export function languageAlternates(siteUrl: string, path: `/${string}`): Record<string, string> {
  return Object.fromEntries(
    locales.map((locale) => [locale, `${siteUrl}${localizedPath(locale, path)}`]),
  );
}

export function normalizeFrameworkPathname(pathname: string): string {
  if (pathname === '/zh-CN') return '/';
  if (pathname.startsWith('/zh-CN/')) return pathname.slice('/zh-CN'.length);
  return pathname;
}

export function formatLocalePath(locale: string, pathname: string): string {
  return pathname === '/' ? `/${locale}` : `/${locale}${pathname}`;
}
