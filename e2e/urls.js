// @ts-check
// 🔗 عناوين بيئة الاختبار — مكان واحد للإعدادات والاختبارات.
const PORT = process.env.E2E_PORT || '8000';
module.exports = {
  // الشركة التجريبية (seed_demo --sub e2e). Chromium بيحوّل أي *.localhost
  // لـ 127.0.0.1 لوحده — مافيش تعديل في /etc/hosts.
  TENANT_URL: process.env.E2E_TENANT_URL || `http://e2e.localhost:${PORT}`,
  // الدومين الرئيسي: صفحة الهبوط والأسعار والتسجيل
  PUBLIC_URL: process.env.E2E_PUBLIC_URL || `http://localhost:${PORT}`,
};
