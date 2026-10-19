// @ts-check
// 🎭 إعدادات اختبارات المتصفح — شوف e2e/README.md لطريقة التشغيل.
const path = require('path');
const { defineConfig, devices } = require('@playwright/test');
const { TENANT_URL } = require('./urls');

/** كل لغة project لوحده: نفس الاختبارات بالعربي (RTL) والإنجليزي (LTR). */
const LANGS = [
  { name: 'ar', locale: 'ar-EG' },
  { name: 'en', locale: 'en-US' },
];

module.exports = defineConfig({
  testDir: './tests',
  timeout: 60_000,
  expect: { timeout: 10_000 },
  fullyParallel: false,
  // شركة واحدة وحد طلبات بالدقيقة للباقة — عاملين كفاية ومايخنقوش السيرفر
  workers: process.env.CI ? 2 : 1,
  retries: process.env.CI ? 1 : 0,
  forbidOnly: !!process.env.CI,
  reporter: process.env.CI ? [['github'], ['html', { open: 'never' }]] : [['list'], ['html', { open: 'never' }]],
  globalSetup: require.resolve('./global-setup'),
  use: {
    baseURL: TENANT_URL,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
    viewport: { width: 1366, height: 900 },
    ...(process.env.E2E_CHROMIUM ? { launchOptions: { executablePath: process.env.E2E_CHROMIUM } } : {}),
  },
  projects: LANGS.map(({ name, locale }) => ({
    name,
    use: {
      ...devices['Desktop Chrome'],
      viewport: { width: 1366, height: 900 },
      locale,
      // نفس كوكي زرار اللغة في الموقع (LocaleMiddleware) — مش بس Accept-Language
      storageState: path.join(__dirname, '.auth', `admin-${name}.json`),
    },
  })),
});
