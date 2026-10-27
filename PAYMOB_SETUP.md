# 💳 إعداد بوابة الدفع Paymob — دليل سريع

## ١. متغيرات البيئة في `.env`

```bash
PAYMOB_API_KEY=<api_key_من_dashboard>
PAYMOB_INTEGRATION_ID=<رقم_integration>
PAYMOB_IFRAME_ID=<رقم_iframe>
PAYMOB_HMAC_SECRET=<مفتاح_HMAC>   # ⚠️ مطلوب للإنتاج
```

> الـ API Key و Integration ID و iFrame ID موجودين بالفعل في `.env`. الناقص هو `PAYMOB_HMAC_SECRET`.

---

## ٢. الحصول على HMAC Secret

1. ادخل [Paymob Dashboard](https://accept.paymob.com/portal2)
2. اذهب لـ **Developers → HMAC Calculator** (أو **Settings → Account Info**)
3. انسخ قيمة **HMAC** السرية
4. ضعها في `.env`:
   ```bash
   PAYMOB_HMAC_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
   ```
5. أعد تشغيل السيرفر عشان يقرا `.env` الجديد (docs/DEPLOY_RUNBOOK.md):
   ```bash
   docker compose --env-file .env up -d
   ```

### لماذا HMAC مهم؟
بدونه أي حد ممكن يبعت request لـ `/payment/paymob/callback/` ويزور دفعة وهمية → اشتراكات بدون مقابل، باقات تصاميم بدون دفع فعلي.

---

## ٣. ضبط Callbacks في Paymob Dashboard

ادخل: **Developers → Payment Integrations → اختر Integration → Edit**

### حقل **Transaction processed callback** (notification_url)
```
https://mousstec.com/payment/paymob/callback/
```
> Paymob يبعت POST request فيه نتيجة الدفع. السيرفر يعالجها ويفعّل الاشتراك/الباقة.

### حقل **Transaction response callback** (redirection_url)
```
https://mousstec.com/payment/paymob/callback/
```
> العميل بيتحوّل لهنا بعد ما يكمل الدفع — السيرفر يعرض له صفحة نجاح/فشل.

### ملاحظة:
السيرفر بيستخدم نفس الـ URL للـ notification و الـ redirection لأن الكود بيتعامل مع GET و POST.

---

## ٤. الاختبار قبل النشر

### أ) في Test Mode
Paymob بيوفر بطاقات اختبار:
- **Visa:** `4111 1111 1111 1111`
- **MasterCard:** `5111 1111 1111 1118`
- CVV: أي ٣ أرقام · تاريخ: أي تاريخ مستقبلي
- OTP إذا طلب: `123456`

### ب) سيناريو اختبار كامل
```bash
1. ادخل: https://mousstec.com/marketplace/design-store/
2. اضغط شراء أي باقة → اختار "بطاقة ائتمان"
3. أدخل بيانات البطاقة الاختبارية
4. تأكد إن `DesignPurchase.status` بقى 'paid' في DB
5. تحقق من log: `[PAYMOB/DESIGN] Purchase #X paid via card`
```

### ج) فحص HMAC شغال
```bash
# يجب ألا ترى هذا التحذير في log:
⚠️ [PAYMOB] PAYMOB_HMAC_SECRET not configured — HMAC verification skipped!
```

---

## ٥. استكشاف الأخطاء

| الخطأ في log | السبب | الحل |
|---|---|---|
| `Auth failed: HTTP 401` | API key غلط/منتهي | جدّد API key من Paymob |
| `Order failed: HTTP 400` | integration_id غلط | تأكد من رقم Integration |
| `Payment key failed` | بيانات billing ناقصة | تحقق من تليفون/إيميل العميل |
| `Paymob timeout` | شبكة بطيئة | زود timeout أو تحقق من firewall |
| `HMAC MISMATCH` | secret غلط أو يحاول حد يزور callback | جدّد HMAC أو اتحقق من IP المهاجم |

---

## ٦. إعدادات الإنتاج النهائية

في الإنتاج لازم تكون كل القيم دي مضبوطة:

```bash
DEBUG=False
BASE_DOMAIN=mousstec.com
PAYMOB_API_KEY=<live_key>
PAYMOB_INTEGRATION_ID=<live_id>
PAYMOB_IFRAME_ID=<live_iframe>
PAYMOB_HMAC_SECRET=<hmac_secret>   # ⚠️ حرج
PAYMOB_CARD_SUBSCRIPTIONS=True     # بعد ما تجرّب في Test Mode (القسم ٧)
```

> تحويل HTTP→HTTPS بيعمله Caddy — `SECURE_SSL_REDIRECT` في `.env` بيتجاهل عمداً
> (كان بيعمل redirect loop). شوف التعليق في `erp_core/settings.py`.

> **تذكير:** ما تحطش الـ HMAC Secret في git أو في public repos. خليه في `.env` فقط، وضيف `.env` لـ `.gitignore`.

---

## ٧. الاشتراكات بالكارت (صفحة الأسعار والتجديد)

زرار **«ادفع بالكارت»** في نافذة الاشتراك بيظهر لما **كل** دول يتحققوا:

| الشرط | ليه |
|---|---|
| `PAYMOB_CARD_SUBSCRIPTIONS=True` | مفتاح تشغيل صريح — مايتفتحش لوحده بعد نشر |
| `PAYMOB_API_KEY` + `PAYMOB_INTEGRATION_ID` (رقم) + `PAYMOB_IFRAME_ID` | بيانات البوابة |
| `PAYMOB_HMAC_SECRET` | من غيره كل إشعارات الدفع بتترفض في الإنتاج ⇒ فلوس من غير تفعيل |
| الموقع المصري | الحساب بالجنيه — الموقع الإماراتي بيفضل «قريباً» |

**إزاي بيشتغل** (`clients/views/subscription_views.py` + `clients/services/platform_billing.py`):

1. العميل يختار الباقة والمدة ⇒ السيرفر يحسب السعر (`Plan.price_for_period`) — نفس الرقم
   اللي ظاهر في الصفحة (الأسعار نفسها جاية من السيرفر).
2. يتعمل طلب على Paymob ⇒ **فاتورة «مصدرة» بمرجع رقم الطلب** (`open_gateway_invoice`) —
   أي طلب قديم ماتدفعش بيتلغي.
3. إشعار Paymob (موقّع HMAC) ⇒ `settle_gateway_invoice`: يتأكد إن المبلغ ≥ الفاتورة، يحسب
   الفترة من تاريخ الدفع، و`mark_paid()` بيمد الاشتراك ويفعّل الشركة. الإشعار المكرر مابيمدّش
   مرتين، ودفعة متأخرة على طلب اتلغى برضه بتتسوّى (الفلوس وصلت).
4. لو حاجة فشلت بعد الدفع: الفاتورة فاضلة «مصدرة» بمرجع Paymob في لوحة المالك
   (فواتير المنصة) — تتسوّى يدوي من غير ما العميل يدفع تاني.

**التجربة قبل التشغيل:** حط مفاتيح الـ Test Mode + `PAYMOB_CARD_SUBSCRIPTIONS=True` على
بيئة تجربة، ادفع بكارت الاختبار (القسم ٤)، واتأكد إن الفاتورة بقت «مدفوعة» وتاريخ الاشتراك اتمد.

## ٨. التجديد التلقائي بالكارت المحفوظ (`PAYMOB_AUTO_RENEW`)

مقفول افتراضياً. لما يتقفل: الكروت مابتتحفظش، والمهمة اليومية مابتعملش حاجة، والشاشة مابتظهرش.

**قبل التشغيل — من Paymob:**
1. فعّل **Card Tokenization** على الـ integration بتاع الدفع بالكارت (العميل هيشوف «احفظ الكارت»
   في صفحة الدفع، وPaymob هيبعت `type=TOKEN` على نفس رابط الـ callback — مفيش رابط جديد).
2. اطلب **MOTO integration** (خصم من غير ما العميل يكون موجود) وحط رقمه في
   `PAYMOB_MOTO_INTEGRATION_ID` — Paymob مابيقبلش الخصم بالـ token على integration عادي.
3. `PAYMOB_AUTO_RENEW=True` + `PAYMOB_HMAC_SECRET` (إشعار الكارت موقّع بحقول تانية غير الدفع).
4. Celery beat شغّال (المهمة `clients.tasks.auto_renew_subscriptions` كل يوم ١٢:٠١ — قبل
   تحذيرات التجديد والتعليق).

**إزاي بيشتغل** (`clients/services/auto_renew.py`):

| الخطوة | التفاصيل |
|---|---|
| حفظ الكارت | إشعار `TOKEN` (بعد التحقق من التوقيع) ⇒ `SavedPaymentCard` على الشركة صاحبة فاتورة الطلب. بنخزّن الـ token وآخر ٤ أرقام بس. كارت واحد نشط — الجديد بيحل محل القديم |
| الموافقة | **العميل بيفعّله بنفسه** من «إدارة الاشتراك» (`/subscription/manage/`) — صاحب الشركة بس. يقدر يوقفه أو يحذف الكارت في أي وقت |
| التذكير | إيميل قبل نهاية الاشتراك بـ ٣ أيام: «هنخصم X ج.م من الكارت •••• 1234» بدل «جدّد» |
| الخصم | قبل النهاية بيوم: فاتورة «مصدرة» بمرجع رقم طلب Paymob **قبل** الخصم ⇒ خصم بالسعر الحالي للباقة ولنفس المدة ⇒ تسوية + تمديد + إيميل بالفاتورة. إشعار Paymob لما يوصل بيلاقيها مدفوعة (مفيش تمديد مرتين) |
| مرة في اليوم | المحاولة بتتسجّل قبل أي اتصال بالبوابة — المهمة لو اشتغلت مرتين مابتخصمش مرتين |
| الرفض | الفاتورة بتتلغي، ومحاولة كل يوم (جوه فترة السماح)، وبعد ٣ رفضات التجديد بيتقفل والعميل بيتبلّغ |
| الرد ضاع وسط الخصم | (timeout / Paymob 5xx) **مفيش محاولة تانية** — الفاتورة بتفضل مستحقة والـ webhook بيسوّيها لو الخصم تم، والتجديد بيتقفل وحدث «خطأ» بيظهر في سجل المنصة عشان حد يراجع على Paymob |

**التجربة في Test Mode:** ادفع اشتراك بكارت الاختبار واختار «احفظ الكارت» ⇒ في «إدارة الاشتراك»
فعّل التجديد ⇒ خلّي تاريخ نهاية الاشتراك بكرة (لوحة المالك) ⇒ شغّل
`python manage.py shell -c "from clients.tasks import auto_renew_subscriptions as t; print(t())"` ⇒
لازم يطبع `{'renewed': 1}` والفاتورة الجديدة «مدفوعة» والتاريخ اتمد.
