import { defineConfig } from '@playwright/test';
export default defineConfig({
  testDir: './tests/browser', workers: 1, fullyParallel: false,
  use: { headless: true, viewport: { width: 1440, height: 960 }, channel: process.env.PLAYWRIGHT_CHANNEL || 'chrome' },
  reporter: 'list', outputDir: 'test-results',
});
