// @ts-check
// 🌐 الصفحات العامة (الدومين الرئيسي، من غير دخول) بالعربي والإنجليزي.
const { test, expect } = require('./fixtures');
const { PUBLIC_URL } = require('../urls');

const PAGES = ['/', '/pricing/', '/features/', '/login/', '/connect/signup/'];

test.use({ baseURL: PUBLIC_URL, storageState: { cookies: [], origins: [] } });

for (const path of PAGES) {
  test(`صفحة عامة ${path}`, async ({ page, lang }) => {
    await page.context().addCookies([{ name: 'django_language', value: lang, url: PUBLIC_URL }]);
    const res = await page.goto(path);
    expect(res?.status(), `${path} رجّع ${res?.status()}`).toBeLessThan(400);
    await page.waitForLoadState('networkidle');
    await expect(page.locator('html')).toHaveAttribute('dir', lang === 'ar' ? 'rtl' : 'ltr');
    const text = await page.locator('body').innerText();
    expect(text, 'نص مكسور ظاهر في الصفحة').not.toMatch(/\bNaN\b|\bundefined\b|\[object Object\]/);
  });
}
