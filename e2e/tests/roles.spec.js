// @ts-check
// 🔐 الصلاحيات من المتصفح: الكاشير بيبيع، لكن شاشات الفلوس والإدارة مقفولة عليه.
const { test, expect } = require('./fixtures');

test.use({ asUser: 'cashier' });

test('الكاشير يفتح نقطة البيع', async ({ page }) => {
  const res = await page.goto('/system/lightning-pos/');
  expect(res?.status()).toBeLessThan(400);
  await expect(page.locator('#search')).toBeVisible();
});

for (const path of ['/system/treasuries/', '/system/reports/pnl/', '/system/staff/']) {
  test(`الكاشير ممنوع من ${path}`, async ({ page, pageErrors }) => {
    const res = await page.goto(path);
    // المنع إما 403 أو تحويل لشاشة تانية — المهم إن الشاشة نفسها ماتفتحش
    const blocked = res?.status() === 403 || !page.url().endsWith(path);
    expect(blocked, `${path} فتحت للكاشير (${res?.status()} ${page.url()})`).toBe(true);
    pageErrors.splice(0);  // الـ 403 هنا هو المتوقّع
  });
}
