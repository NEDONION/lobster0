import '@fontsource-variable/instrument-sans';
import '@fontsource/ibm-plex-mono/400.css';
import '@/styles/globals.css';

import Link from 'next/link';
import type { Metadata } from 'next';

import { siteFacts } from '@/content/site';

export const metadata: Metadata = {
  description: 'The requested MiniClaw route could not be found.',
  metadataBase: new URL('https://miniclaw.vercel.app'),
  robots: { follow: false, index: false },
  title: 'Route not found — MiniClaw',
};

export default function GlobalNotFound() {
  return (
    <html lang="zh-CN">
      <body>
        <main className="not-found-page">
          <div className="not-found-page__code" aria-hidden="true">404</div>
          <div className="not-found-page__content">
            <p className="section-kicker">ROUTE_NOT_FOUND</p>
            <h1>这条 Trace 没有找到目标。</h1>
            <p>The route ended before RESULT_DELIVERED. Return to a known MiniClaw surface.</p>
            <div>
              <Link className="button button--primary" href="/">返回官网</Link>
              <Link className="button button--secondary" href="/docs">阅读文档</Link>
              <a className="button button--secondary" href={siteFacts.links.github}>GitHub ↗</a>
            </div>
          </div>
        </main>
      </body>
    </html>
  );
}
