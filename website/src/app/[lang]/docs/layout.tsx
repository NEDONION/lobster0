import { DocsLayout as FumadocsLayout } from 'fumadocs-ui/layouts/docs';
import { notFound } from 'next/navigation';
import type { ReactNode } from 'react';

import { getLocale } from '@/lib/i18n';
import { baseOptions } from '@/lib/layout.shared';
import { source } from '@/lib/source';

export default async function DocsLayout({
  children,
  params,
}: {
  children: ReactNode;
  params: Promise<{ lang: string }>;
}) {
  const locale = getLocale((await params).lang);
  if (!locale) notFound();

  return (
    <FumadocsLayout
      {...baseOptions(locale)}
      sidebar={{ defaultOpenLevel: 1, prefetch: false }}
      tree={source.getPageTree(locale)}
    >
      {children}
    </FumadocsLayout>
  );
}
