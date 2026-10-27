# 🎭 اختبارات المتصفح (Playwright)

بتفتح السيستم في Chromium حقيقي — **بالعربي (RTL) والإنجليزي (LTR)** — وبتفشل لو أي
صفحة فيها خطأ JavaScript أو `console.error` أو رد سيرفر ≥ 500 (أو ≥ 400 من الموقع
نفسه)، أو لو ظهر في الصفحة `NaN` / `undefined` / `[object Object]`.

اتعملت بعد المراجعة الشاملة: أغلب المشاكل اللي اتلقت يدوي (أرقام مكسورة في JS
بالإنجليزي، صفحات بتطلع 500، زرار «دفع الباقي»…) كانت هتتمسك هنا أوتوماتيك.
وأول ما اشتغلت مسكت نقطة البيع وفاتورة الشراء ميتين بالإنجليزي.

## الملفات

| الملف | بيختبر إيه |
|---|---|
| `tests/screens.spec.js` | ١٧ شاشة أساسية (لوحة التحكم، نقطة البيع، الأصناف، الفواتير، العملاء، الموردين، المشتريات، الخزائن، المصروفات، كارت الصيانة، التقارير، الموظفين، الحضور…) |
| `tests/pos-sale.spec.js` | بيع كاش حقيقي من نقطة البيع لصنف بـ ١٬٢٥٠ (يثبت إن «دفع الباقي» صح) والفاتورة تظهر في القائمة |
| `tests/roles.spec.js` | الكاشير بيبيع، بس الخزائن/الأرباح/الموظفين مقفولين عليه |
| `tests/public.spec.js` | الصفحات العامة (الرئيسية، الأسعار، المميزات، الدخول، التسجيل) |
| `tests/fixtures.js` | الأساس: تجميع أخطاء المتصفح + `expectHealthyPage()` |
| `global-setup.js` | دخول مرة واحدة لكل مستخدم وحفظ الجلسة (الموقع بيقفل بعد محاولات كتير) |

## التشغيل محلياً

```bash
# ١) السيرفر (تيرمنال لوحده) — بيجهّز القاعدة + شركة تجريبية e2e + ستاتيك + daphne
export SECRET_KEY=local-dev-key DATABASE_URL=postgres://postgres:123@localhost:5432/erp_db
E2E_PASSWORD=Secret123 scripts/e2e_server.sh

# ٢) الاختبارات
cd e2e
npm ci
npx playwright install chromium        # مرة واحدة
E2E_PASSWORD=Secret123 npx playwright test
npx playwright test --project=en       # الإنجليزي بس
npx playwright test --headed           # تشوف المتصفح
npx playwright show-report             # تقرير HTML (فيديو + trace لأي اختبار فشل)
```

الشركة التجريبية على `http://e2e.localhost:8000` — Chromium بيحوّل أي `*.localhost`
لجهازك لوحده. الحسابات: `admin` / `cashier` / `tech` / `accountant` بنفس `E2E_PASSWORD`
(اتعملوا بـ `python manage.py seed_demo --sub e2e`).

## إضافة اختبار

- شاشة جديدة؟ ضيفها لـ `SCREENS` في `tests/screens.spec.js` — سطر واحد.
- تدفّق (بيع/شراء/تحصيل)؟ ملف `*.spec.js` جديد، و`require('./fixtures')` بدل
  `@playwright/test` عشان تاخد فحص الأخطاء أوتوماتيك.
- اختار العناصر بـ `id` أو `data-*` مش بالنص — النص بيتغيّر بين العربي والإنجليزي.
- ضوضاء من برّه كودنا (CDN مثلاً) تتضاف لـ `IGNORED_CONSOLE` في `fixtures.js` **مع سبب**.

## في CI

job `e2e` في `.github/workflows/ci.yml` بيشغّل نفس الكلام على كل PR. لو فشل،
التقرير (فيديو + screenshot + trace) بيترفع كـ artifact اسمه `playwright-report`.
