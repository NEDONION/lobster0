import { describe, expect, it } from 'vitest';

import robots from '@/app/robots';
import sitemap from '@/app/sitemap';

describe('public metadata', () => {
  it('publishes every locale home and docs page', () => {
    const entries = sitemap();
    const urls = entries.map((entry) => entry.url);
    expect(urls).toContain('https://lobster0.jchu.tech/');
    expect(urls).toContain('https://lobster0.jchu.tech/en');
    expect(urls).toContain('https://lobster0.jchu.tech/ja');
    expect(urls).toContain('https://lobster0.jchu.tech/ko');
    expect(urls).toContain('https://lobster0.jchu.tech/fr');
    expect(urls).toContain('https://lobster0.jchu.tech/docs');
    expect(urls).toContain('https://lobster0.jchu.tech/en/docs');
    expect(urls).toContain('https://lobster0.jchu.tech/fr/docs');
    // 5 locales x 7 pages (home + 6 docs)
    expect(urls).toHaveLength(35);
    expect(new Set(urls).size).toBe(35);

    // every entry advertises all five hreflang alternates
    const home = entries.find((entry) => entry.url === 'https://lobster0.jchu.tech/');
    expect(Object.keys(home?.alternates?.languages ?? {})).toEqual([
      'zh-CN',
      'en',
      'ja',
      'ko',
      'fr',
    ]);
  });

  it('advertises the sitemap and keeps APIs out of search results', () => {
    const policy = robots();
    expect(policy.sitemap).toBe('https://lobster0.jchu.tech/sitemap.xml');
    expect(policy.rules).toEqual(
      expect.objectContaining({
        allow: '/',
        disallow: '/api/',
      }),
    );
  });

  it('pairs every localized sitemap entry with hreflang alternates', () => {
    for (const entry of sitemap()) {
      expect(entry.alternates?.languages).toEqual(
        expect.objectContaining({ en: expect.any(String), 'zh-CN': expect.any(String) }),
      );
    }
  });
});
