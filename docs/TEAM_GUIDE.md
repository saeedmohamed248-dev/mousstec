# 👥 دليل التخصصات — كل مبرمج يشتغل فين

الدليل ده عشان أي مبرمج جديد يعرف في ساعة: **تخصصه في أنهي فولدر، يبدأ منين،
بيختبر إزاي، وإيه الحاجات اللي ممنوع يكسرها.** القواعد العامة في
[CONTRIBUTING.md](../CONTRIBUTING.md)، والتشغيل في [README.md](../README.md)،
وخريطة الجداول في [DATABASE.md](DATABASE.md) (متولّدة من الموديلات).

> القاعدة الذهبية في التقسيم: **موديول لكل مجال**. الملف الضخم بيتقسم لحزمة
> (`printing/models/`، `printing/admin/`، `clients/views/saas_admin_views/`…)
> و`__init__.py` بيصدّر نفس الأسماء القديمة — فمحدش برا الحزمة بيحس بالتقسيم.
> اختبار `erp_core.tests.test_codebase_structure` بيمنع أي ملف جديد يعدّي ١٢٠٠ سطر.

---

## 🗺️ الخريطة السريعة

| التخصص | الفولدرات | ابدأ من | اختباراته |
|---|---|---|---|
| 💰 المحاسبة والفلوس | `inventory/services/` (accounting/invoice/treasury/customer_payments)، `inventory/models/{finance,invoices,accounting,returns}.py`، `printing/ledger.py`، `hr/services/payroll_service.py` | [ACCOUNTING_CYCLE.md](ACCOUNTING_CYCLE.md) | `make test-app APP=inventory` + `manage.py audit_books` |
| 🏪 المخزن ونقطة البيع والورشة | `inventory/views_lightning/` (pos, sales, purchases, products…)، `inventory/views/`، `workshop/` | `inventory/views_lightning/__init__.py` | `inventory/tests/test_business_day_e2e.py` |
| 🖨️ المطابع | `printing/` (models/ admin/ views/ ledger.py)، `print_market/` | `printing/models/__init__.py` (جدول الموديولات) | `make test-app APP=printing` |
| 🏢 المنصة والاشتراكات | `clients/models/tenancy.py`، `tenancy/`، `billing/`، `clients/views/{auth,subscription}_views.py`، `clients/services/platform_billing.py` | [PAYMOB_SETUP.md](../PAYMOB_SETUP.md) | `make test-app APP=tenancy` و `APP=billing` |
| 👑 لوحة مالك المنصة | `clients/views/saas_admin_views/` (`/superadmin/`)، `clients/views/admin_views/` | `clients/views/saas_admin_views/__init__.py` | `make test-app APP=clients` |
| 🛒 السوق B2B / C2C | `marketplace_b2b/`، `marketplace_c2c/`، `clients/models/marketplace_*.py`، `clients/views/{b2b,parts_marketplace,marketplace_core,merchant_parts}_views.py` | [clients/views/README.md](../clients/views/README.md) | `make test-app APP=marketplace_b2b` |
| 🤖 الذكاء الاصطناعي | `erp_core/ai/`، `ai_rooms/`، `design_store/`، `clients/views/design_store_views/`، `omnichannel/`، `messenger_bot/`، `social_ads/` | `erp_core/ai/` | `make test-app APP=design_store` |
| 🚗 التشخيص والسيارات | `smart_diagnostics/`، `bmw_ecu/`، `repair_atlas/`، `diagnostics_catalog/` | `bmw_ecu/` / `smart_diagnostics/services/` | `make test-app APP=bmw_ecu` |
| 🦾 الروبوت والكشك (IoT) | `robot/` (Django)، `smart_robot/` (FastAPI منفصل بـ pytest) | `robot/README.md` | `make test-app APP=robot` + `cd smart_robot && pytest` |
| 👥 الموارد البشرية | `hr/` | `hr/services/` | `make test-app APP=hr` |
| 🎨 الواجهة والترجمة | `templates/`، `<app>/templates/`، `static/`، `locale/`، `tailwind.config.js` | [I18N.md](I18N.md) | `make lint` + `make css-check` + `make e2e` |
| 📱 الموبايل | `mobile_api/` (REST `/api/mobile/v1/`)، `mobile_app/` (Flutter) | `mobile_app/README.md` | `make test-app APP=mobile_api` |
| 🚀 DevOps | `deploy/`، `Dockerfile`، `docker-compose.yml`، `.github/workflows/` | [DEPLOY_RUNBOOK.md](DEPLOY_RUNBOOK.md) | CI على الـ PR |
| 🧪 الجودة (QA) | `<app>/tests/`، `e2e/` | `inventory/tests/base.py` (`ERPTenantTestCase`) | `make test-shard SHARD=n` |

---

## 🗄️ الداتابيز في ٥ سطور

1. PostgreSQL واحدة: schema `public` للمنصة، و**schema لكل شركة** (django-tenants).
2. `SHARED_APPS` ⇒ جداول في `public` (clients, blog, print_market…)، `TENANT_APPS` ⇒ جوه كل شركة (inventory, printing, hr…).
   الجدول الكامل في [DATABASE.md](DATABASE.md).
3. `migrate` مقفول — `make migrate`. بعد أي تغيير موديل: `makemigrations` + `make db-map` واعمل commit للاتنين.
4. جدول في `public` **ممنوع** يعمل FK لجدول شركة (اختبار `tenancy.tests.test_db_map` بيمسكها).
5. نقل موديل بين تطبيقات = `SeparateDatabaseAndState` و`db_table` ثابت (الخطوات في [ARCHITECTURE.md](ARCHITECTURE.md) §8).

---

## ➕ إزاي أضيف حاجة جديدة (بالخطوات)

### شاشة جديدة
1. الـ view في موديول مجالها (مثلاً شاشة خزنة ⇒ `inventory/views_lightning/treasuries.py`؛ شاشة سوبر أدمن ⇒ الموديول المناسب في `clients/views/saas_admin_views/`).
2. لو الحزمة ليها `__init__.py` بيصدّر أسماء: ضيف الاسم في بلوك موديوله.
3. الرابط في `urls.py` بتاع التطبيق (أو `erp_core/urls.py` للمنصة).
4. `@login_required` + `@role_required(...)` / `module_required` — وضيفها لـ `SCREENS` في `e2e/tests/screens.spec.js`.
5. أي نص ⇒ `{% trans %}` + ترجمته + `make messages`.

### موديل (جدول) جديد
1. في موديول المجال جوه `models/` (مثلاً `printing/models/treasury.py`)، وضيف الاسم لـ `models/__init__.py`.
2. FK لموديل في موديول تاني في نفس الحزمة: `from .customers import PrintCustomer` فوق؛
   ولو هيعمل استيراد دائري استخدم اسم نصّي `'printing.PrintCustomer'` أو استيراد جوه الدالة.
3. `makemigrations` ⇒ `make migrate` ⇒ `make db-map`.
4. الفلوس؟ الحركة تعدي على الخدمات (`AccountingService` / `FinancialTransaction` / `ledger.sync_txn`) + اختبار يثبت إن الدفاتر متوازنة.

### خدمة (منطق) جديدة
`<app>/services/<concern>.py` — الـ view بتنادي الخدمة، والخدمة مابتعرفش حاجة عن الـ request.

---

## 🧭 مين بيراجع إيه

`.github/CODEOWNERS` متقسم بنفس التخصصات اللي فوق. لما تيجي تعيّن مبرمج على تخصص
بدّل اسم المالك في السطر بتاعه بحسابه على GitHub — GitHub هيطلب مراجعته أوتوماتيك
على أي PR بيلمس فولدراته، والفلوس بتفضل محتاجة مراجعة المالك.
