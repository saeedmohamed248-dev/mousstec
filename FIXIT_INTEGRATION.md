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

---

# 🛡️ حارس المرتجعات بالصور + بصمة القطعة بالذكاء الاصطناعي

القطع المستعملة لازم نتأكد إنها **قطعتنا** ومتلعبش فيها قبل ما نقبل إرجاعها.
الحل: نصوّر القطعة وقت الصرف، والذكاء الاصطناعي يستخرج **بصمة مرئية** لها
(علامات مميزة، أرقام تسلسلية ظاهرة، حالة السطح). لما ترجع نصوّرها تاني
ونقارن — لو مش نفس القطعة أو فيها تلف جديد يقول **مش هتنفع ترجع + السبب**.

## إزاي شغال؟

| الحدث | اللي بيحصل |
|---|---|
| بيع سطر قطعة **مستعملة/كور** | يتفتح "حارس مرتجعات" تلقائياً بحالة *بانتظار التصوير* (signal 6.6) |
| المحل يصوّر القطعة وقت الصرف | الذكاء الاصطناعي يعمل **بصمة صرف** ويثبّتها (مصدر الحقيقة) |
| العميل يرجّع القطعة | تصوير الراجع → مقارنة بالبصمة → حكم: مقبول / مرفوض (+ الأسباب) / مراجعة بشرية |
| العميل على الموقع | يصوّر القطعة **قبل الشرا** (baseline) و**عند الإرجاع** ويعرف فوراً تنفع ترجع ولا لأ وليه |

**سياسة أمان:** لو الذكاء الاصطناعي مطفي أو غير واثق، الحكم يبقى *مراجعة بشرية*
— مفيش قبول أو رفض أعمى.

## نقاط النهاية

### ويب هوك الموقع (حماية `X-Sync-Secret`)
`POST /inventory/webhooks/fixit/return/verify/`
```jsonc
{ "sku": "PART-123", "order": "1044", "phone": "010...",
  "stage": "pre" | "post",          // pre = قبل الشرا، post = عند الإرجاع
  "images": ["<base64>", "..."] }
```
- `pre` → يحفظ صور العميل كـ baseline (ويعمل بصمة لو مفيش بصمة محل).
- `post` → يرجّع الحكم: `{ ok, returnable, match_score, reasons: [...], message }`.

### واجهات المحل الداخلية (تسجيل دخول مطلوب — `multipart/form-data` بحقل `image`)
- `POST /inventory/returns/fingerprint/<item_id>/` — تصوير الصرف (بصمة).
- `POST /inventory/returns/verify/<guard_id>/` — فحص المرتجع (الحكم).

## الإعداد

مفيش مفاتيح جديدة إلزامية — الحارس بيستخدم مفتاح رؤية Gemini الموجود
(`AI_VISION_API_KEY` / `GEMINI_API_KEY`) و `ENABLE_AI_PREDICTIONS=1`.
لإبلاغ الموقع بالحكم تلقائياً (اختياري): `FIXIT_RETURN_STATUS_URL`
(لو فاضية بيقع على `FIXIT_SYNC_URL` بـ `action: "return_status"`).

## الملفات

- `inventory/models/returns.py` — `PartReturnGuard` + `PartReturnPhoto`
- `inventory/ai_services.py` — `fingerprint_part_image` + `verify_part_return`
- `inventory/services/return_verification.py` — التنسيق + إبلاغ الموقع
- `inventory/views/fixit_returns.py` — ويب هوك الموقع + واجهات المحل
- `inventory/admin/returns.py` — متابعة الحرّاس والصور والأحكام من لوحة التحكم
