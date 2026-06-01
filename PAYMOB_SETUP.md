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
5. أعد تشغيل السيرفر:
   ```bash
   sudo systemctl restart mousstec
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
SECURE_SSL_REDIRECT=True
```

> **تذكير:** ما تحطش الـ HMAC Secret في git أو في public repos. خليه في `.env` فقط، وضيف `.env` لـ `.gitignore`.
