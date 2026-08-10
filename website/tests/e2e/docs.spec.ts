import { expect, test, type Locator } from '@playwright/test';

async function firstVisible(locator: Locator): Promise<Locator> {
  for (let index = 0; index < (await locator.count()); index += 1) {
    const candidate = locator.nth(index);
    if (await candidate.isVisible()) return candidate;
  }
  throw new Error('No visible locator matched');
}

test('Chinese docs expose navigation, search, and evidence boundaries', async ({ isMobile, page }) => {
  const pageErrors: string[] = [];
  page.on('pageerror', (error) => pageErrors.push(error.message));
  await page.goto('/docs');

  await expect(page.getByRole('heading', { level: 1 })).toHaveText('从这里开始');
  await expect(page.locator('blockquote').filter({ hasText: 'Implementation PASS' })).toContainText(
    'Live PASS',
  );

  if (isMobile) {
    const sidebarButton = await firstVisible(page.getByRole('button', { name: '打开侧边栏' }));
    await sidebarButton.click();
    await expect(await firstVisible(page.getByRole('link', { exact: true, name: '运行时' }))).toBeVisible();
    await page.locator('#nd-sidebar-mobile').getByRole('button', { name: '关闭侧边栏' }).click();
    await expect(page.locator('#nd-sidebar-mobile')).not.toBeVisible();
  } else {
    await expect(await firstVisible(page.getByRole('link', { exact: true, name: '运行时' }))).toBeVisible();
  }

  const searchButton = await firstVisible(page.getByRole('button', { name: '打开搜索' }));
  await searchButton.click();
  const searchDialog = page.getByRole('dialog');
  await searchDialog.getByRole('textbox').fill('安全');
  await expect(searchDialog.getByRole('button', { name: /安全边界/ }).first()).toBeVisible();
  expect(pageErrors).toEqual([]);
});

test('English docs keep locale-specific navigation and search results', async ({ isMobile, page }) => {
  await page.goto('/en/docs');

  await expect(page.getByRole('heading', { level: 1 })).toHaveText('Start here');
  await expect(page.locator('blockquote').filter({ hasText: 'Implementation PASS' })).toContainText(
    'Live PASS',
  );

  if (isMobile) {
    const sidebarButton = await firstVisible(page.getByRole('button', { name: 'Open Sidebar' }));
    await sidebarButton.click();
    await expect(await firstVisible(page.getByRole('link', { exact: true, name: 'Runtime' }))).toBeVisible();
    await page.locator('#nd-sidebar-mobile').getByRole('button', { name: 'Close Sidebar' }).click();
    await expect(page.locator('#nd-sidebar-mobile')).not.toBeVisible();
  } else {
    await expect(await firstVisible(page.getByRole('link', { exact: true, name: 'Runtime' }))).toBeVisible();
  }

  const searchButton = await firstVisible(page.getByRole('button', { name: 'Open Search' }));
  await searchButton.click();
  const searchDialog = page.getByRole('dialog');
  await searchDialog.getByRole('textbox').fill('Runtime');
  await expect(searchDialog.getByRole('button', { name: /Runtime/ }).first()).toBeVisible();
});
