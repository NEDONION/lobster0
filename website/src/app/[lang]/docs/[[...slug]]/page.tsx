import {
  DocsBody,
  DocsDescription,
  DocsPage,
  DocsTitle,
} from 'fumadocs-ui/layouts/docs/page';
import type { Metadata } from 'next';
import { notFound } from 'next/navigation';

import { getMDXComponents } from '@/components/docs/mdx-components';
import { getLocale } from '@/lib/i18n';
import { source } from '@/lib/source';

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
  if (!page) return {};

  return {
    description: page.data.description,
    title: `${page.data.title} — MiniClaw`,
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
