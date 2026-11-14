// @ts-check
// 🔧 أمر شغل بإجمالي ≥ ١٠٠٠ + «دفع كامل الإجمالي»: الزرار كان بياخد النص المتنسّق
// «1,250.00» فخانة المبلغ تفضل فاضية والأمر يتحفظ آجل بالكامل من غير تنبيه.
const { test, expect } = require('./fixtures');

test('أمر شغل: دفع كامل الإجمالي لمبلغ فوق الألف', async ({ page }) => {
  await page.goto('/system/job-card/');
  await page.fill('#cust-name', 'عميل أمر شغل');
  await page.fill('#cust-phone', '01055544433');
  await page.fill('#part-search', 'DEMO-OIL-5W30');
  await page.locator('#part-results > div').first().click();
  await expect(page.locator('#t-total')).toHaveText('1,250.00');

  await page.selectOption('#treasury', { index: 1 });
  await page.locator('button[onclick="payFull()"]').click();
  await expect(page.locator('#paid')).toHaveValue('1250.00');
  await expect(page.locator('#due-display')).toHaveValue('0.00');

  const [saved] = await Promise.all([
    page.waitForResponse((r) => r.url().includes('/job-card/save/')),
    page.click('#save-btn'),
  ]);
  const data = await saved.json();
  expect(data.ok, JSON.stringify(data)).toBe(true);
  expect(data.paid).toBe(1250);
  expect(data.due).toBe(0);
});
