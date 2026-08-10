import { expect, test } from '@playwright/test';

const contentPaths = [
  '/',
  '/en',
  '/docs',
  '/docs/getting-started',
  '/docs/runtime',
  '/docs/security',
  '/docs/channels',
  '/docs/memory',
  '/en/docs',
  '/en/docs/getting-started',
  '/en/docs/runtime',
  '/en/docs/security',
  '/en/docs/channels',
  '/en/docs/memory',
] as const;

test('all content routes, same-origin links, and fragments resolve', async ({ isMobile, page, request }) => {
  test.setTimeout(60_000);
  test.skip(isMobile, 'The route crawl is viewport-independent and runs once in desktop Chromium.');

  for (const publicPath of ['/sitemap.xml', '/robots.txt', '/opengraph-image']) {
    const response = await request.get(publicPath);
    expect(response.status(), `${publicPath} should be publicly reachable`).toBe(200);
  }

  const missingResponse = await request.get('/missing-route');
  expect(missingResponse.status()).toBe(404);
  await page.goto('/missing-route');
  await expect(page.getByRole('heading', { level: 1 })).toContainText('Trace');

  const discovered = new Map<string, Set<string>>();
  for (const path of contentPaths) {
    const response = await request.get(path);
    expect(response.status(), `${path} should resolve`).toBeLessThan(400);

    await page.goto(path);
    const hrefs = await page.locator('a[href]').evaluateAll((anchors) =>
      anchors.map((anchor) => (anchor as HTMLAnchorElement).href),
    );
    discovered.set(path, new Set(hrefs));
  }

  const checkedUrls = new Set<string>();
  for (const [sourcePath, hrefs] of discovered) {
    for (const href of hrefs) {
      const url = new URL(href);
      if (url.origin !== 'http://127.0.0.1:3000') continue;
      const requestUrl = `${url.pathname}${url.search}`;
      if (!checkedUrls.has(requestUrl)) {
        checkedUrls.add(requestUrl);
        const response = await request.get(requestUrl);
        expect(response.status(), `${sourcePath} links to ${requestUrl}`).toBeLessThan(400);
      }

      if (!url.hash) continue;
      await page.goto(`${url.pathname}${url.search}${url.hash}`);
      const fragmentExists = await page.evaluate((hash) => {
        const id = decodeURIComponent(hash.slice(1));
        return Boolean(document.getElementById(id));
      }, url.hash);
      expect(fragmentExists, `${sourcePath} links to missing fragment ${url.hash}`).toBe(true);
    }
  }
});
