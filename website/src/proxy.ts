import { createI18nMiddleware } from 'fumadocs-core/i18n/middleware';
import { NextResponse, type NextFetchEvent, type NextRequest } from 'next/server';

import { formatLocalePath, i18n, isI18nBypassPath } from '@/lib/i18n';

const rewrittenRequestHeader = 'x-miniclaw-i18n-rewrite';
const i18nMiddleware = createI18nMiddleware({ ...i18n, format: formatLocalePath });

export default async function proxy(request: NextRequest, event: NextFetchEvent) {
  if (isI18nBypassPath(request.nextUrl.pathname)) return NextResponse.next();
  if (request.headers.get(rewrittenRequestHeader) === '1') return NextResponse.next();

  const response = await i18nMiddleware(request, event);
  const rewriteDestination = response?.headers.get('x-middleware-rewrite');
  if (!rewriteDestination) return response;

  const requestHeaders = new Headers(request.headers);
  requestHeaders.set(rewrittenRequestHeader, '1');

  return NextResponse.rewrite(new URL(rewriteDestination), {
    request: { headers: requestHeaders },
  });
}

export const config = {
  matcher: ['/((?!api|_next/static|_next/image|favicon.svg|images).*)'],
};
