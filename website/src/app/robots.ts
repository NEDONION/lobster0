import type { MetadataRoute } from 'next';

export default function robots(): MetadataRoute.Robots {
  return {
    rules: {
      allow: '/',
      disallow: '/api/',
      userAgent: '*',
    },
    sitemap: 'https://lobster0.vercel.app/sitemap.xml',
  };
}
