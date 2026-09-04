# 📱 mobile_api — واجهة REST شاملة لتطبيقات الموبايل (Android / iOS)

تطبيق Django يعرّض طبقة API مخصّصة لتطبيق Flutter (`mobile_app/`) تغطّي **كل موديولات
النظام**. يعمل داخل سياق المستأجر (django-tenants) فتُعزل البيانات تلقائياً حسب الورشة.

البنية: حزمة `serializers/` و `views/` مقسّمة حسب المجال (core / crm / inventory /
purchasing / workshop / finance / hr / diagnostics).

## المصادقة ولوحة المعلومات

| الغرض | الطريقة | المسار |
|------|---------|--------|
| تسجيل الدخول (JWT + المستخدم) | POST | `auth/login/` |
| تحديث التوكن | POST | `auth/refresh/` |
| المستخدم الحالي | GET | `auth/me/` |
| لوحة معلومات شاملة | GET | `dashboard/` |

## الموارد (تحت `/api/mobile/v1/`)

| الموديول | المسار | العمليات |
|---------|--------|----------|
| أوامر الشغل (صيانة) | `work-orders/` | list/retrieve/create + `‹id›/status/` |
| سجلات الإصلاح | `repair-logs/` | list |
| تقارير التشخيص | `diagnostic-reports/` | list |
| قطع الغيار | `products/` | **CRUD** + `low-stock/` |
| تنبيهات المخزون | `stock-alerts/` | list |
| تحويلات المخزون | `stock-transfers/` | list/retrieve/create |
| حركات المخزون | `inventory-movements/` | list |
| الموردون | `vendors/` | **CRUD** |
| فواتير الشراء | `purchase-invoices/` | list/retrieve |
| كتالوج الخدمات | `services/` | **CRUD** |
| عمليات التفكيك | `scrap-jobs/` | list/retrieve |
| العملاء | `customers/` | **CRUD** + `‹id›/vehicles/` |
| المركبات | `vehicles/` | **CRUD** |
| عقود الصيانة | `maintenance-contracts/` | **CRUD** |
| تذكيرات الصيانة | `service-nudges/` | list |
| تقييمات العملاء | `customer-feedback/` | list |
| الخزائن | `treasuries/` | **CRUD** |
| الحركات المالية | `transactions/` | list/retrieve/create |
| فئات المصروفات | `expense-categories/` | **CRUD** |
| الفروع | `branches/` | **CRUD** |
| الموظفون | `employees/` | list/retrieve |
| الحضور والانصراف | `attendance/` | list |
| طلبات الإجازة | `leave-requests/` | list/retrieve/create |
| السلف | `advances/` | list/retrieve/create |
| مسيّرات الرواتب | `payroll-runs/` | list/retrieve |
| بنود الرواتب | `payroll-entries/` | list |
| أجهزة الفحص | `diag-devices/` | list/retrieve |
| الفحوصات | `diag-scans/` | list/retrieve |
| سجل الأعطال | `fault-logs/` | list |

**CRUD** = list / retrieve / create / update (PATCH) / delete.

## قرارات تصميمية

- **CRUD كامل** مُفعّل على البيانات الرئيسية التي يحرّرها المستخدم بيده (العملاء،
  المركبات، الموردون، المنتجات، الخدمات، الفروع، العقود، الخزائن، فئات المصروفات).
- السجلات **المولّدة/المحسوبة** (حركات المخزون، كشوف الرواتب، الحضور، الأعطال،
  الفحوصات، تنبيهات المخزون) **للقراءة فقط** — تعديلها بالإيد يفسد سلامة الحسابات
  والمخزون.
- الحذف محميّ: محاولة حذف سجل مرتبط بفواتير/حركات تُرجع `409` بدل خطأ 500.
- كل المسارات (عدا `auth/login` و `auth/refresh`) تتطلب `Authorization: Bearer`.
- «أمر الشغل» = `SaleInvoice` نوع `maintenance`، حالاته:
  `quotation → in_progress → quality_check → ready → posted`.

## الاختبارات

```bash
python manage.py test mobile_api
```

تغطّي المصادقة، لوحة المعلومات، أوامر الشغل (وتحديث الحالة)، المخزون والبحث ونقص
المخزون، إنشاء العملاء/أوامر الشغل/الحركات المالية، قوائم كل الموديولات، وحماية
المسارات — داخل tenant حقيقي.
