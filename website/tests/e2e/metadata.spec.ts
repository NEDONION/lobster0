import { expect, test } from '@playwright/test';

test('publishes the lobster brand in social and browser metadata', async ({ page, request }) => {
  await page.goto('/en');

  await expect(page.locator('meta[property="og:image:alt"]')).toHaveAttribute(
    'content',
    'Lobster0 — Your local agent, ready to act.',
  );

  const image = await request.get('/opengraph-image');
  expect(image.ok()).toBe(true);
  expect(image.headers()['content-type']).toContain('image/png');
  expect((await image.body()).byteLength).toBeGreaterThan(10_000);

  const iconHref = await page.locator('link[rel="icon"]').getAttribute('href');
  expect(iconHref).toBeTruthy();
  const icon = await request.get(iconHref!);
  expect(icon.ok()).toBe(true);
  expect(await icon.text()).toContain('\u{1F99E}');
});
