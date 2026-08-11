import type { MetadataRoute } from 'next';

import { siteFacts } from '@/content/site';
import { languageAlternates, locales, localizedPath } from '@/lib/i18n';

const siteUrl = siteFacts.siteUrl;
const docsSlugs = ['', 'getting-started', 'runtime', 'security', 'channels', 'memory'];

function absolute(path: string) {
  return path === '/' ? `${siteUrl}/` : `${siteUrl}${path}`;
}

export default function sitemap(): MetadataRoute.Sitemap {
  const paths = ['/', ...docsSlugs.map((slug) => (slug ? `/docs/${slug}` : '/docs'))];

  return paths.flatMap((path) => {
    // Every locale variant of this page points at the same alternates map so
    // search engines can pick the right language for each visitor.
    const languages = languageAlternates(siteUrl, path as `/${string}`);

    return locales.map((locale) => ({
      alternates: { languages },
      changeFrequency: 'weekly' as const,
      priority: locale === 'zh-CN' ? (path === '/' ? 1 : 0.8) : path === '/' ? 0.9 : 0.7,
      url: absolute(localizedPath(locale, path as `/${string}`)),
    }));
  });
}
