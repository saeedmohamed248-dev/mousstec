// @ts-check
// 🖥️ الشاشات الأساسية بالعربي والإنجليزي (المالك/admin): كل شاشة لازم تفتح
// من غير أخطاء JS/سيرفر، باتجاه صح، ومن غير أرقام أو نصوص مكسورة.
const { test, expectHealthyPage } = require('./fixtures');

const SCREENS = [
  ['لوحة التحكم', '/system/dashboard/'],
  ['نقطة البيع السريعة', '/system/lightning-pos/'],
  ['الأصناف', '/system/products/'],
  ['إضافة صنف سريع', '/system/quick-product/'],
  ['فواتير البيع', '/system/invoices/'],
  ['العملاء والمديونيات', '/system/customers/'],
  ['الموردين', '/system/vendors/'],
  ['المشتريات', '/system/purchases/'],
  ['فاتورة شراء جديدة', '/system/purchases/new/'],
  ['الخزائن', '/system/treasuries/'],
  ['المصروفات', '/system/expenses/'],
  ['كارت صيانة', '/system/job-card/'],
  ['التقارير', '/system/reports/'],
  ['الأرباح والخسائر', '/system/reports/pnl/'],
  ['ميزان المراجعة', '/system/reports/trial-balance/'],
  ['المحاسبة والإقفال', '/system/accounting/'],
  ['دفتر الأستاذ (النقدية)', '/system/accounting/ledger/1001/'],
  ['الموظفين', '/system/staff/'],
  ['الحضور والانصراف', '/hr/attendance/'],
];

for (const [title, path] of SCREENS) {
  test(`${title} ${path}`, async ({ page, lang }) => {
    const res = await page.goto(path);
    test.expect(res?.status(), `${path} رجّع ${res?.status()}`).toBeLessThan(400);
    await page.waitForLoadState('networkidle');
    await expectHealthyPage(page, lang);
  });
}
