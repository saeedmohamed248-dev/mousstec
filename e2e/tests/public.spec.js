// @ts-check
// 🌐 الصفحات العامة (الدومين الرئيسي، من غير دخول) بالعربي والإنجليزي.
const { test, expect } = require('./fixtures');
const { PUBLIC_URL } = require('../urls');

const PAGES = ['/', '/pricing/', '/features/', '/login/', '/connect/signup/', '/account/recovery/'];

test.use({ baseURL: PUBLIC_URL, storageState: { cookies: [], origins: [] } });

/** كل نص عربي ظاهر (نص/placeholder/title/aria-label/اختيارات) — غير زرار اللغة «ع». */
async function visibleArabic(page) {
  return page.evaluate(() => {
    const ar = /[؀-ۿ]/;
    const out = new Set();
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    while (walker.nextNode()) {
      const node = walker.currentNode;
      const text = node.textContent.trim();
      const el = node.parentElement;
      if (!text || !ar.test(text) || !el || el.offsetParent === null || ['SCRIPT', 'STYLE'].includes(el.tagName)) continue;
      if (text === 'ع' && el.closest('form, button')) continue;   // زرار تبديل اللغة للعربي
      out.add(text.slice(0, 80));
    }
    document.querySelectorAll('[placeholder],[title],[aria-label],option').forEach((el) => {
      for (const attr of ['placeholder', 'title', 'aria-label']) {
        const value = el.getAttribute(attr);
        if (value && ar.test(value)) out.add(`${attr}: ${value.slice(0, 80)}`);
      }
      if (el.tagName === 'OPTION' && ar.test(el.textContent)) out.add(`option: ${el.textContent.trim().slice(0, 80)}`);
    });
    return [...out];
  });
}

for (const path of PAGES) {
  test(`صفحة عامة ${path}`, async ({ page, lang }) => {
    await page.context().addCookies([{ name: 'django_language', value: lang, url: PUBLIC_URL }]);
    const res = await page.goto(path);
    expect(res?.status(), `${path} رجّع ${res?.status()}`).toBeLessThan(400);
    await page.waitForLoadState('networkidle');
    await expect(page.locator('html')).toHaveAttribute('dir', lang === 'ar' ? 'rtl' : 'ltr');
    const text = await page.locator('body').innerText();
    expect(text, 'نص مكسور ظاهر في الصفحة').not.toMatch(/\bNaN\b|\bundefined\b|\[object Object\]/);
    if (lang === 'en') {
      // الموقع الإنجليزي مايبقاش فيه نص عربي (نسيان {% trans %} أو ترجمة ناقصة في django.po)
      expect(await visibleArabic(page), 'نص عربي في الصفحة الإنجليزي').toEqual([]);
    }
  });
}
