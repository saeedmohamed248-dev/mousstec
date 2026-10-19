// @ts-check
// 💰 بيع كاش حقيقي من نقطة البيع: صنف سعره ≥ ١٠٠٠ عشان «دفع الباقي» يتحسب صح
// (كان بيقرا «1,250.00» كـ ١) ⇒ الفاتورة مدفوعة بالكامل وبتظهر في قائمة الفواتير.
const { test, expect, expectHealthyPage } = require('./fixtures');

test('بيع كاش كامل من نقطة البيع', async ({ page, context, lang }) => {
  await page.goto('/system/lightning-pos/');
  await page.fill('#search', 'DEMO-OIL-5W30');
  await page.locator('#results [data-pid]').first().click();
  await expect(page.locator('#cart-body tr')).toHaveCount(1);
  await expect(page.locator('#cart-total')).toHaveText('1,250.00');

  await page.click('#pay-full');
  await expect(page.locator('.pay-line .pay-amt').first()).toHaveValue('1250.00');
  await expect(page.locator('#t-due')).toHaveText('0.00');
  const treasury = page.locator('.pay-line .pay-tre').first();
  if (!(await treasury.inputValue())) {
    await treasury.selectOption({ index: 1 });
  }

  const [checkout, receipt] = await Promise.all([
    page.waitForResponse((r) => r.url().includes('/lightning-pos/checkout/')),
    context.waitForEvent('page'),
    page.click('#checkout'),
  ]);
  const data = await checkout.json();
  expect(data.ok, JSON.stringify(data)).toBe(true);
  await receipt.waitForLoadState();
  expect(receipt.url()).toContain(String(data.invoice_id));
  await receipt.close();

  // الفاتورة في القائمة ومدفوعة بالكامل
  await page.goto('/system/invoices/');
  await expectHealthyPage(page, lang);
  await expect(page.locator('body')).toContainText(`#${data.invoice_id}`);
});
