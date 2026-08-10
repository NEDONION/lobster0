import type { MetadataRoute } from 'next';

import { siteFacts } from '@/content/site';

export default function robots(): MetadataRoute.Robots {
  return {
    rules: {
      allow: '/',
      disallow: '/api/',
      userAgent: '*',
    },
    sitemap: `${siteFacts.siteUrl}/sitemap.xml`,
  };
}
