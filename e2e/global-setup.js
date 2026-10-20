// @ts-check
// 🔑 دخول مرة واحدة لكل مستخدم وحفظ الجلسة (.auth/<user>-<lang>.json) —
// الموقع بيقفل الدخول بعد محاولات كتير لنفس الحساب، فمش هندخل في كل اختبار.
const fs = require('fs');
const path = require('path');
const { chromium } = require('@playwright/test');
const { TENANT_URL: tenantUrl, PUBLIC_URL: publicUrl } = require('./urls');

const USERS = ['admin', 'cashier'];
const LANGS = ['ar', 'en'];

module.exports = async () => {
  const password = process.env.E2E_PASSWORD;
  if (!password) throw new Error('E2E_PASSWORD مش متظبط — نفس كلمة سر seed_demo');
  const authDir = path.join(__dirname, '.auth');
  fs.mkdirSync(authDir, { recursive: true });
  const executablePath = process.env.E2E_CHROMIUM || undefined;
  const browser = await chromium.launch({ executablePath });
  try {
    for (const user of USERS) {
      const ctx = await browser.newContext();
      const page = await ctx.newPage();
      await page.goto(`${tenantUrl}/login/`);
      await page.fill('input[name=email]', user);
      await page.fill('input[name=password]', password);
      await Promise.all([
        page.waitForURL((u) => !u.pathname.startsWith('/login'), { timeout: 30_000 }),
        page.press('input[name=password]', 'Enter'),
      ]);
      for (const lang of LANGS) {
        // كوكي اللغة (زي زرار تغيير اللغة) على دومين الشركة والدومين العام
        await ctx.addCookies([tenantUrl, publicUrl].map((url) => ({ name: 'django_language', value: lang, url })));
        await ctx.storageState({ path: path.join(authDir, `${user}-${lang}.json`) });
      }
      await ctx.close();
    }
  } finally {
    await browser.close();
  }
};
