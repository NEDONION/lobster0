import { defineI18n } from 'fumadocs-core/i18n';

import type { Locale } from '@/content/site';

export const locales = ['zh-CN', 'en'] as const satisfies readonly Locale[];

export const localeNames: Record<Locale, string> = {
  'zh-CN': '简体中文',
  en: 'English',
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
  fallbackLanguage: null,
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
  return path === '/' ? '/en' : `/en${path}`;
}

export function normalizeFrameworkPathname(pathname: string): string {
  if (pathname === '/zh-CN') return '/';
  if (pathname.startsWith('/zh-CN/')) return pathname.slice('/zh-CN'.length);
  return pathname;
}

export function formatLocalePath(locale: string, pathname: string): string {
  return pathname === '/' ? `/${locale}` : `/${locale}${pathname}`;
}
