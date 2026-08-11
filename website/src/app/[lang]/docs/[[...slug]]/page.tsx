import {
  DocsBody,
  DocsDescription,
  DocsPage,
  DocsTitle,
} from 'fumadocs-ui/layouts/docs/page';
import type { Metadata } from 'next';
import { notFound } from 'next/navigation';

import { getMDXComponents } from '@/components/docs/mdx-components';
import { siteFacts } from '@/content/site';
import { getLocale, languageAlternates, localizedPath } from '@/lib/i18n';
import { source } from '@/lib/source';

const siteUrl = siteFacts.siteUrl;

interface PageProps {
  params: Promise<{ lang: string; slug?: string[] }>;
}

export function generateStaticParams() {
  return source.generateParams();
}

export async function generateMetadata({ params }: PageProps): Promise<Metadata> {
  const { lang, slug } = await params;
  const locale = getLocale(lang);
  if (!locale) return {};
  const page = source.getPage(slug, locale);
  // Keep metadataBase even on the not-found path, otherwise Next falls back to
  // localhost when resolving social image URLs.
  if (!page) return { metadataBase: new URL(siteUrl) };
  const path = slug?.length ? `/docs/${slug.join('/')}` : '/docs';
  const canonicalPath = localizedPath(locale, path as `/${string}`);

  return {
    alternates: {
      canonical: `${siteUrl}${canonicalPath}`,
      languages: languageAlternates(siteUrl, path as `/${string}`),
    },
    description: page.data.description,
    metadataBase: new URL(siteUrl),
    openGraph: { url: `${siteUrl}${canonicalPath}` },
    title: page.data.title,
  };
}

export default async function DocsContentPage({ params }: PageProps) {
  const { lang, slug } = await params;
  const locale = getLocale(lang);
  if (!locale) notFound();
  const page = source.getPage(slug, locale);
  if (!page) notFound();

  const MDX = page.data.body;

  return (
    <DocsPage toc={page.data.toc} tableOfContent={{ style: 'clerk' }}>
      <DocsTitle>{page.data.title}</DocsTitle>
      <DocsDescription>{page.data.description}</DocsDescription>
      <DocsBody>
        <MDX components={getMDXComponents()} />
      </DocsBody>
    </DocsPage>
  );
}
