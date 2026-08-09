'use client';

import { FrameworkProvider, type Framework } from 'fumadocs-core/framework';
import { RootProvider as BaseRootProvider } from 'fumadocs-ui/provider/base';
import NextImage from 'next/image';
import NextLink from 'next/link';
import { useParams, usePathname, useRouter } from 'next/navigation';
import type { ComponentProps } from 'react';

import { normalizeFrameworkPathname } from '@/lib/i18n';

type AppProviderProps = ComponentProps<typeof BaseRootProvider>;

function usePublicPathname(): string {
  return normalizeFrameworkPathname(usePathname() ?? '/');
}

export function AppProvider(props: AppProviderProps) {
  return (
    <FrameworkProvider
      Image={NextImage as Framework['Image']}
      Link={NextLink as Framework['Link']}
      useParams={useParams}
      usePathname={usePublicPathname}
      useRouter={useRouter}
    >
      <BaseRootProvider {...props} />
    </FrameworkProvider>
  );
}
