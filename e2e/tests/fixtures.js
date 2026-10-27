// @ts-check
// 🧰 أساس كل الاختبارات: أي خطأ JavaScript أو console.error أو رد ≥ 500 (أو ≥ 400
// من نفس الموقع) في أي صفحة بيفشّل الاختبار — ده اللي كان هيمسك أغلب المشاكل
// اللي اتصلحت في المراجعة (أرقام مكسورة في JS، صفحات 500 بالإنجليزي...).
const path = require('path');
const base = require('@playwright/test');

// ضوضاء معروفة مش من كودنا — أي إضافة هنا لازم سبب مكتوب جنبها.
const IGNORED_CONSOLE = [
  /favicon\.ico/,                       // المتصفح بيطلبها لوحده
  /Failed to load resource: net::ERR_(NAME_NOT_RESOLVED|INTERNET_DISCONNECTED|CONNECTION_REFUSED|TUNNEL_CONNECTION_FAILED)/, // CDN خارجي في بيئة من غير نت
];

/** @param {string} url @param {string} origin */
const sameOrigin = (url, origin) => { try { return new URL(url).origin === origin; } catch { return false; } };

const test = base.test.extend({
  /** المستخدم اللي الاختبار شغّال بيه (الافتراضي admin من الإعدادات) */
  asUser: [null, { option: true }],
  storageState: async ({ asUser, storageState }, use, testInfo) => {
    await use(asUser ? path.join(__dirname, '..', '.auth', `${asUser}-${testInfo.project.name}.json`) : storageState);
  },
  lang: async ({}, use, testInfo) => { await use(testInfo.project.name); },
  pageErrors: [async ({ page, baseURL }, use) => {
    const errors = [];
    const origin = new URL(baseURL || 'http://localhost').origin;
    page.on('pageerror', (e) => errors.push(`JS error on ${page.url()}: ${e.message}`));
    page.on('console', (m) => {
      if (m.type() !== 'error') return;
      const where = (m.location() || {}).url || '';
      const text = m.text();
      if (IGNORED_CONSOLE.some((re) => re.test(text) || re.test(where))) return;
      if (where && !sameOrigin(where, origin) && /Failed to load resource/.test(text)) return; // CDN خارجي
      errors.push(`console.error on ${page.url()}: ${text} @ ${where}`);
    });
    page.on('response', (r) => {
      const status = r.status();
      if (status >= 500 || (status >= 400 && sameOrigin(r.url(), origin) && !/favicon/.test(r.url()))) {
        errors.push(`HTTP ${status} ${r.request().method()} ${r.url()} (page ${page.url()})`);
      }
    });
    await use(errors);
    base.expect(errors, 'المتصفح سجّل أخطاء').toEqual([]);
  }, { auto: true }],
});

/**
 * فحوصات مشتركة لأي شاشة: اتفتحت من غير ما تطلع برة، اتجاه الصفحة صح للغة،
 * ومافيش «NaN» أو «undefined» ظاهرة (علامة رقم/متغير اتكسر في JS أو القالب).
 * @param {import('@playwright/test').Page} page
 * @param {string} lang
 */
async function expectHealthyPage(page, lang) {
  await base.expect(page).not.toHaveURL(/\/login\//);
  await base.expect(page.locator('html')).toHaveAttribute('dir', lang === 'ar' ? 'rtl' : 'ltr');
  const text = await page.locator('body').innerText();
  base.expect(text, 'نص مكسور ظاهر في الصفحة').not.toMatch(/\bNaN\b|\bundefined\b|\[object Object\]/);
}

module.exports = { test, expect: base.expect, expectHealthyPage };
