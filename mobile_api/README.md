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
| لوحة معلومات شاملة (ورش) | GET | `dashboard/` |
| تسجيل/إلغاء جهاز إشعارات | POST / DELETE | `devices/` |

> رد `auth/login/` و `auth/me/` فيه `tenant`: اسم الشركة، **القطاع** (`automotive` / `printing`)،
> العملة، اللغة، اللوجو — وللمطابع `permissions` (صلاحيات الموظف). التطبيق بيبني نفسه عليها.
> كل مسار مقفول على قطاعه: مسارات الورش ترجع 403 للمطبعة والعكس (`IndustryGate`).

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

## 🎨 المطابع (تحت `/api/mobile/v1/print/`)

الصلاحيات = صلاحيات Django الموديل (جروبات «فريق العمل» في الويب): القراءة `view_*` والكتابة `add/change_*`.
الكتابة بتعدّي على نفس دوال الويب — مفيش قيود أو حسابات جديدة.

| الموديول | المسار | العمليات |
|---------|--------|----------|
| لوحة المطبعة | `print/dashboard/` | get (فواتير النهارده لمن يشوف الخزنة بس) |
| الطلبات/الفواتير | `print/orders/` | list (`?status=open`) / retrieve / **create** (= الفاتورة السريعة) |
| إجراءات الطلب | `print/orders/‹id›/action/` | `confirm` (حد الائتمان) / `deliver` (= زرار «اتسلّمت») / `cancel` (من غير مدفوع بس) |
| مهام الإنتاج | `print/jobs/` | list (الشغالة) / retrieve / `‹id›/advance/` (بيحترم موافقة البروفة) / `‹id›/proofs/` |
| العملاء | `print/customers/` | list / retrieve / create / update — من غير حذف أو رصيد افتتاحي |
| الخامات | `print/materials/` | list (`?low=1`) / retrieve / `low-stock/` |
| عروض الأسعار | `print/quotations/` | list / retrieve (+ رابط المشاركة) |
| البروفات | `print/proofs/` | list / retrieve (+ رابط موافقة العميل) |
| الماكينات | `print/machines/` | list / retrieve |
| الخزائن (للتحصيل) | `print/treasuries/` | get — من غير أرصدة |

## 🔔 الإشعارات (Firebase Cloud Messaging)

`MobileDevice` (جوه schema كل شركة) + `push.py` (FCM HTTP v1 من غير مكتبات جديدة) + `signals.py`:
رد العميل على البروفة ⇒ فريق الإنتاج، تنبيه نقص مخزون ⇒ المخزن والإدارة، طلب إجازة ⇒ الـ HR.
الإرسال بعد الـ commit وفي Celery، وأي خطأ فيه مابيوقّعش العملية الأصلية.
الإعداد: `FCM_SERVICE_ACCOUNT_JSON` (أو `FCM_SERVICE_ACCOUNT_FILE`) — من غيره الإشعارات بتتسجّل في اللوج بس.

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
python manage.py test mobile_api            # كله
python manage.py test mobile_api.test_printing   # المطابع + القطاع + الإشعارات
```

تغطّي المصادقة، لوحة المعلومات، أوامر الشغل (وتحديث الحالة)، المخزون والبحث ونقص
المخزون، إنشاء العملاء/أوامر الشغل/الحركات المالية، قوائم كل الموديولات، وحماية
المسارات — داخل tenant حقيقي.
