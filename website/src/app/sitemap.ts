import type { MetadataRoute } from 'next';

const siteUrl = 'https://lobster0.vercel.app';
const docsSlugs = ['', 'getting-started', 'runtime', 'security', 'channels', 'memory'];

function absolute(path: string) {
  return path === '/' ? `${siteUrl}/` : `${siteUrl}${path}`;
}

export default function sitemap(): MetadataRoute.Sitemap {
  const paths = ['/', ...docsSlugs.map((slug) => (slug ? `/docs/${slug}` : '/docs'))];

  return paths.flatMap((path) => {
    const zh = path;
    const en = path === '/' ? '/en' : `/en${path}`;
    const languages = {
      'zh-CN': absolute(zh),
      en: absolute(en),
    };

    return [
      {
        alternates: { languages },
        changeFrequency: 'weekly' as const,
        priority: path === '/' ? 1 : 0.8,
        url: absolute(zh),
      },
      {
        alternates: { languages },
        changeFrequency: 'weekly' as const,
        priority: path === '/' ? 0.9 : 0.7,
        url: absolute(en),
      },
    ];
  });
}
