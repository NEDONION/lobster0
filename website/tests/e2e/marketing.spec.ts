import { expect, test } from '@playwright/test';

test('marketing page keeps its dense three-screen interaction contract', async ({
  context,
  isMobile,
  page,
}) => {
  const pageErrors: string[] = [];
  page.on('pageerror', (error) => pageErrors.push(error.message));
  await context.grantPermissions(['clipboard-read', 'clipboard-write'], {
    origin: 'http://127.0.0.1:3000',
  });

  await page.goto('/');

  await expect(page.locator('main > section.marketing-section')).toHaveCount(3);
  await expect(page.locator('[data-brand-mark]').first()).toBeVisible();
  await expect(page.locator('.hero-surface-card')).toHaveCount(4);
  await expect(page.getByRole('tablist').nth(0).getByRole('tab')).toHaveCount(5);
  await expect(page.getByRole('tablist').nth(1).getByRole('tab')).toHaveCount(3);

  const heroSize = await page
    .locator('#hero-title')
    .evaluate((node) => parseFloat(getComputedStyle(node).fontSize));
  const sectionSize = await page
    .locator('#product-title')
    .evaluate((node) => parseFloat(getComputedStyle(node).fontSize));
  const signalDuration = await page
    .locator('.hero-network__signal')
    .first()
    .evaluate((node) => parseFloat(getComputedStyle(node).animationDuration));
  expect(heroSize).toBeLessThanOrEqual(54);
  expect(sectionSize).toBeLessThanOrEqual(36);
  expect(signalDuration).toBeGreaterThan(0);

  await page.goto('/#safety');
  await expect(page.locator('#safety-tab')).toHaveAttribute('aria-selected', 'true');
  await expect(page.locator('#safety-panel')).toBeVisible();
  await page.locator('#channels-tab').click();
  await expect(page).toHaveURL(/#channels$/);
  await expect(page.locator('#channels-tab')).toHaveAttribute('aria-selected', 'true');

  await page.locator('#external-cli-tab').click();
  await expect(page.locator('#external-cli-panel')).toBeVisible();
  await expect(page).toHaveURL(/#external-cli$/);

  await page.locator('.command-copy button').first().click();
  await expect(page.locator('.command-copy button').first()).toContainText('已复制');

  if (isMobile) {
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    );
    const productTitleWidth = await page.locator('#product-title').evaluate(
      (node) => node.getBoundingClientRect().width,
    );
    expect(overflow).toBe(0);
    expect(productTitleWidth).toBeGreaterThan(250);
  } else {
    const viewportHeights = await page.evaluate(
      () => document.documentElement.scrollHeight / window.innerHeight,
    );
    expect(viewportHeights).toBeLessThanOrEqual(3.2);
  }

  await page.getByRole('link', { exact: true, name: 'English' }).click();
  await expect(page).toHaveURL('/en');
  await expect(page.getByRole('heading', { level: 1 })).toHaveText('Your local agent, ready to act.');
  expect(pageErrors).toEqual([]);
});

test('reduced motion renders the final trace and static tab state immediately', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await page.goto('/#memory');

  await expect(page.locator('.claw-trace li').last()).toHaveAttribute('aria-current', 'step');
  await expect(page.locator('.claw-trace li').last()).toHaveAttribute('data-state', 'active');
  await expect(page.locator('#memory-panel')).toHaveAttribute('data-reduced-motion', 'true');
  const signalDuration = await page
    .locator('.hero-network__signal')
    .first()
    .evaluate((node) => parseFloat(getComputedStyle(node).animationDuration));
  expect(signalDuration).toBeLessThanOrEqual(0.001);
});
