# 📱 mobile_api — واجهة REST لتطبيقات الموبايل (Android / iOS)

تطبيق Django يعرّض طبقة API نظيفة ومخصّصة لتطبيق Flutter (`mobile_app/`). يعمل
داخل سياق المستأجر (django-tenants) فتُعزل البيانات تلقائياً حسب الورشة.

## المسارات (البادئة: `/api/mobile/v1/`)

| الغرض | الطريقة | المسار |
|------|---------|--------|
| تسجيل الدخول (JWT + بيانات المستخدم) | POST | `auth/login/` |
| تحديث التوكن | POST | `auth/refresh/` |
| المستخدم الحالي | GET | `auth/me/` |
| لوحة المعلومات | GET | `dashboard/` |
| أوامر الشغل (قائمة/بحث/فلترة) | GET | `work-orders/?status=open&search=...` |
| تفاصيل أمر شغل | GET | `work-orders/{id}/` |
| تغيير الحالة | POST | `work-orders/{id}/status/` `{ "status": "ready" }` |
| المخزون | GET | `products/?search=...` |
| نقص المخزون | GET | `products/low-stock/` |
| تفاصيل قطعة | GET | `products/{id}/` |
| تنبيهات المخزون | GET | `stock-alerts/` |
| العملاء | GET | `customers/?search=...` |
| تفاصيل عميل (+ مركباته) | GET | `customers/{id}/` |

## ملاحظات

- كل المسارات (عدا `auth/login` و `auth/refresh`) تتطلب `Authorization: Bearer <access>`.
- «أمر الشغل» = `SaleInvoice` من نوع `maintenance`؛ حالاته:
  `quotation → in_progress → quality_check → ready → posted`.
- لا تُعرض الحقول الحسّاسة (التكلفة، الأرباح، العمولات) في أي مخرجات.
- المصادقة تعتمد على `rest_framework_simplejwt` وإعدادات `SIMPLE_JWT` القائمة.

## الاختبارات

```bash
python manage.py test mobile_api
```

تغطّي الاختبارات المصادقة، لوحة المعلومات، أوامر الشغل (بما فيها تحديث الحالة والتحقق
من القيم)، المخزون والبحث ونقص المخزون، العملاء، وحماية كل المسارات — داخل tenant حقيقي.
