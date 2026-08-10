import { describe, expect, it } from 'vitest';

import robots from '@/app/robots';
import sitemap from '@/app/sitemap';

describe('public metadata', () => {
  it('publishes localized homes and docs', () => {
    const urls = sitemap().map((entry) => entry.url);
    expect(urls).toContain('https://miniclaw.vercel.app/');
    expect(urls).toContain('https://miniclaw.vercel.app/en');
    expect(urls).toContain('https://miniclaw.vercel.app/docs');
    expect(urls).toContain('https://miniclaw.vercel.app/en/docs');
    expect(urls).toHaveLength(14);
  });

  it('advertises the sitemap and keeps APIs out of search results', () => {
    const policy = robots();
    expect(policy.sitemap).toBe('https://miniclaw.vercel.app/sitemap.xml');
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
