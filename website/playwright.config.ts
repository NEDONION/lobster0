import { defineConfig, devices } from '@playwright/test';

const localChrome = process.env.CI ? {} : { channel: 'chrome' as const };

export default defineConfig({
  expect: { timeout: 10_000 },
  fullyParallel: false,
  projects: [
    {
      name: 'desktop-chromium',
      use: { ...devices['Desktop Chrome'], ...localChrome, viewport: { height: 900, width: 1440 } },
    },
    {
      name: 'mobile-chromium',
      use: {
        ...devices['Pixel 7'],
        ...localChrome,
        viewport: { height: 844, width: 390 },
      },
    },
  ],
  reporter: process.env.CI ? 'github' : 'list',
  testDir: './tests/e2e',
  use: {
    baseURL: 'http://127.0.0.1:3000',
    screenshot: 'only-on-failure',
    trace: 'retain-on-failure',
  },
  webServer: {
    command: 'npm run dev -- --webpack --hostname 127.0.0.1',
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
    url: 'http://127.0.0.1:3000',
  },
  workers: 1,
});
