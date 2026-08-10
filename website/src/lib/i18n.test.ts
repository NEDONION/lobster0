import { describe, expect, it } from 'vitest';

import {
  formatLocalePath,
  getLocale,
  isI18nBypassPath,
  localizedPath,
  normalizeFrameworkPathname,
} from './i18n';

describe('marketing locale helpers', () => {
  it('accepts every supported locale and rejects the rest', () => {
    expect(getLocale('zh-CN')).toBe('zh-CN');
    expect(getLocale('en')).toBe('en');
    expect(getLocale('ja')).toBe('ja');
    expect(getLocale('ko')).toBe('ko');
    expect(getLocale('fr')).toBe('fr');
    expect(getLocale('de')).toBeNull();
  });

  it('keeps Chinese prefix-free and prefixes every other locale', () => {
    expect(localizedPath('zh-CN', '/')).toBe('/');
    expect(localizedPath('zh-CN', '/docs')).toBe('/docs');
    expect(localizedPath('en', '/')).toBe('/en');
    expect(localizedPath('en', '/docs')).toBe('/en/docs');
    expect(localizedPath('ja', '/')).toBe('/ja');
    expect(localizedPath('ko', '/docs')).toBe('/ko/docs');
    expect(localizedPath('fr', '/')).toBe('/fr');
  });

  it('rewrites the root without a trailing slash redirect', () => {
    expect(formatLocalePath('zh-CN', '/')).toBe('/zh-CN');
    expect(formatLocalePath('en', '/docs')).toBe('/en/docs');
  });

  it('leaves framework metadata and static assets outside locale routing', () => {
    expect(isI18nBypassPath('/sitemap.xml')).toBe(true);
    expect(isI18nBypassPath('/robots.txt')).toBe(true);
    expect(isI18nBypassPath('/opengraph-image')).toBe(true);
    expect(isI18nBypassPath('/favicon.svg')).toBe(true);
    expect(isI18nBypassPath('/docs')).toBe(false);
  });

  it('normalizes the hidden default locale for server and client hydration', () => {
    expect(normalizeFrameworkPathname('/zh-CN')).toBe('/');
    expect(normalizeFrameworkPathname('/zh-CN/docs/runtime')).toBe('/docs/runtime');
    expect(normalizeFrameworkPathname('/en/docs/runtime')).toBe('/en/docs/runtime');
  });
});
