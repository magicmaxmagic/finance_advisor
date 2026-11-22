import { defineConfig } from '@playwright/test'

export default defineConfig({
  testDir: './e2e',
  timeout: 120_000,
  expect: { timeout: 75_000 },
  outputDir: '../data/reports/ui-v4/playwright',
  workers: 1,
  use: {
    baseURL: process.env.FINLAB_E2E_URL ?? 'http://127.0.0.1:5173',
    channel: 'chrome',
    launchOptions: process.env.FINLAB_CHROME_PATH ? { executablePath: process.env.FINLAB_CHROME_PATH } : {},
    viewport: { width: 1440, height: 1024 },
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
})
