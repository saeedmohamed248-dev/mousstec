# 🔗 ربط Mouss Tec بموقع FixIt الإلكتروني

المخزون واحد في الاتجاهين — Mouss Tec هو مصدر الحقيقة.

## إزاي شغال؟

| الحدث | اللي بيحصل تلقائياً |
|---|---|
| بيع/شراء/نقل/تسوية أي قطعة في Mouss Tec | الكمية الجديدة تتبعت للموقع فوراً (signal على جدول Inventory) |
| عميل يعمل أوردر على الموقع | الموقع يخصم مخزونه + يبعت الطلب هنا **كفاتورة مسودة (عرض سعر)** باسم العميل ورقمه — تعتمدها فيخصم المخزون رسمياً ويتبعت للموقع |
| منتج جديد أو تعديل أسعار | أمر واحد يزامن الكتالوج كله: `python manage.py fixit_sync_all` |

**المطابقة بين السيستمين بالـ `part_number` هنا = `SKU` في الموقع.**

## خطوات التفعيل (مرة واحدة)

1. **في بيئة Mouss Tec** أضف متغيرين:
   ```
   FIXIT_SYNC_URL=https://your-site.vercel.app/api/sync
   FIXIT_SYNC_SECRET=نفس-قيمة-SYNC_SECRET-في-Vercel
   FIXIT_BRANCH_ID=1   # (اختياري) الفرع اللي فواتير الموقع تتسجل عليه
   ```

2. **في Vercel (الموقع)** أضف:
   ```
   SYNC_SECRET=نفس-القيمة
   MOUSSTEC_WEBHOOK_URL=https://your-mousstec-domain.com/inventory/webhooks/fixit/order/
   MOUSSTEC_SECRET=نفس-القيمة
   ```

3. **مزامنة أولى كاملة** (بترفع كل منتجاتك النشطة للموقع):
   ```
   python manage.py fixit_sync_all
   # مع django-tenants:
   python manage.py tenant_command fixit_sync_all --schema=<اسم_التينانت>
   ```

خلاص — من هنا ورايح كل حاجة تلقائية. لو الشبكة وقعت لحظة إرسال،
العملية الأساسية مش بتتأثر (الإرسال في خيط منفصل والفشل بيتسجل في اللوج فقط)،
وتقدر في أي وقت تعمل `fixit_sync_all` لإعادة التطابق الكامل.

## الملفات

- `inventory/services/fixit_sync.py` — منطق الإرسال للموقع
- `inventory/signals.py` — القسم 6.5: الإرسال التلقائي مع كل حركة مخزون
- `inventory/views/fixit_webhook.py` — استقبال طلبات الموقع كفواتير مسودة
- `inventory/management/commands/fixit_sync_all.py` — المزامنة الكاملة
