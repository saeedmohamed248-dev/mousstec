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

## ٨. التجديد الأوتوماتيك — التصميم (محتاج قرار قبل التنفيذ)

الموجود: `charge_with_saved_token()` و`verify_paymob_token_hmac()` في `billing/services/paymob.py`.
الناقص عشان التجديد يبقى أوتوماتيك:

1. **من Paymob:** تفعيل *Card Tokenization* على الـ integration، و*MOTO integration* منفصل
   للخصم من غير العميل (رقمه في `PAYMOB_MOTO_INTEGRATION_ID`).
2. **موديل + مهاجرة:** `SavedPaymentCard` (الشركة، التوكن، آخر ٤ أرقام، النوع، فعّال) +
   `TenantSubscription.auto_renew`.
3. **إشعار TOKEN:** نفس الـ callback، لما `type == 'TOKEN'` ⇒ يتحقق بـ `verify_paymob_token_hmac`
   ويحفظ الكارت على الشركة صاحبة الطلب.
4. **مهمة يومية (Celery beat):** الشركات اللي `auto_renew` وفاضل يوم على اشتراكها ⇒
   `open_gateway_invoice` + `charge_with_saved_token` بنفس المرجع، والتسوية بتحصل من نفس الإشعار.
   فشل الخصم ⇒ إيميل للمالك + محاولة تانية بعد يومين، وبعد ٣ مرات يتقفل التجديد.
5. **الشاشة:** في «إدارة الاشتراك»: الكارت المحفوظ، تشغيل/إيقاف التجديد، وحذف الكارت.

ده تغيير في سلوك الفلوس (خصم من غير ما العميل يدوس) — محتاج موافقة صاحب المشروع
وحساب Paymob فيه MOTO قبل ما يتنفّذ.
