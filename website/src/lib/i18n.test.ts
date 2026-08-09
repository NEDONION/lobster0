import { describe, expect, it } from 'vitest';

import {
  formatLocalePath,
  getLocale,
  isI18nBypassPath,
  localizedPath,
  normalizeFrameworkPathname,
} from './i18n';

describe('marketing locale helpers', () => {
  it('rejects unknown route locales', () => {
    expect(getLocale('zh-CN')).toBe('zh-CN');
    expect(getLocale('en')).toBe('en');
    expect(getLocale('fr')).toBeNull();
  });

  it('keeps Chinese prefix-free and prefixes English', () => {
    expect(localizedPath('zh-CN', '/')).toBe('/');
    expect(localizedPath('zh-CN', '/docs')).toBe('/docs');
    expect(localizedPath('en', '/')).toBe('/en');
    expect(localizedPath('en', '/docs')).toBe('/en/docs');
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
