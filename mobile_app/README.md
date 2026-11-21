# 📱 Mouss Tec Mobile (Flutter — Android & iOS)

تطبيق موبايل واحد بكود Flutter مشترك يعمل على **أندرويد** و **iOS**، يتصل بنظام
Mouss Tec ERP عبر واجهة **Mobile API** (`/api/mobile/v1/`).

**التطبيق بيتشكّل حسب نشاط الشركة** (السيرفر بيرجّع `tenant.industry` مع الدخول):

| | 🚗 الورش وقطع الغيار | 🎨 المطابع والتصميم |
|---|---|---|
| التابات | الرئيسية • أوامر الشغل • المخزون • المزيد | الرئيسية • الطلبات • الإنتاج • المزيد |
| الرئيسية | أوامر مفتوحة، إيراد اليوم، نقص مخزون… | طلبات مفتوحة/متأخرة، بروفات مستنية العميل، خامات ناقصة، فواتير النهارده |
| أهم الشاشات | أوامر الشغل وتغيير حالتها، باركود، PDF | فاتورة جديدة (زي «الفاتورة السريعة»)، تأكيد/تسليم/إلغاء، لوحة الإنتاج (تصميم ← بروفة ← طباعة ← تشطيب) |
| الصلاحيات | أدوار الموظفين على السيرفر | صلاحيات «فريق العمل» — الأقسام والزراير بتختفي من غير صلاحيتها |

وفي الاتنين:

- 🔐 **الدخول بكود الشركة** (`myshop` ⇒ `https://myshop.mousstec.com`) أو أي رابط/دومين خاص.
- 🌐 **عربي (RTL) وإنجليزي (LTR)** — من الإعدادات أو شاشة الدخول (`lib/core/i18n.dart` + `l10n_en.dart`).
- 💱 **عملة الشركة** من السيرفر (ج.م / ر.س / د.إ …).
- 🔔 **إشعارات Firebase**: رد العميل على البروفة، نقص مخزون، طلب إجازة جديد (راجع `firebase/README.md`).
- JWT مع تخزين آمن (Keychain / Keystore) وتحديث تلقائي للتوكن.

### محرك CRUD العام

معظم الموديولات مبنيّة على **محرّك عام declarative**: كل موديول مُعرّف في
`lib/core/modules.dart` (حقوله، صلاحياته، أعمدة عرضه)، وتتولّى الشاشات العامة
(`generic_list_screen` / `generic_detail_screen` / `generic_form_screen`) توليد
القوائم والتفاصيل والنماذج (مع منتقيات علاقات وتواريخ) تلقائياً — إضافة موديول
جديد = إضافة تعريف واحد فقط. البيانات المولّدة تلقائياً (الرواتب/الحضور/الأعطال…)
معروضة للقراءة فقط حفاظاً على سلامة الحسابات.

---

## 🧱 المعمارية

```
lib/
├── core/           # الأساسيات: ثوابت، عميل HTTP، تخزين التوكن، الثيم
│   ├── api_client.dart   # طبقة HTTP + تحديث توكن تلقائي عند 401 + أخطاء عربية
│   ├── auth_store.dart   # تخزين آمن للتوكن + رابط الخادم
│   ├── constants.dart
│   └── theme.dart
├── models/         # نماذج البيانات (immutable) + تحويل JSON
├── services/       # ApiService: استدعاءات مكتوبة للـ endpoints
├── providers/      # AuthProvider (ChangeNotifier) — حالة المصادقة
├── screens/        # الشاشات: login, dashboard, work orders, inventory, settings
├── widgets/        # عناصر واجهة مشتركة (بطاقات، شارات حالة، حالات تحميل/خطأ)
├── app.dart        # MaterialApp + توطين + بوابة التوجيه حسب المصادقة
└── main.dart       # نقطة الدخول

test/               # اختبارات وحدة + ويدجت (نماذج، عميل API، تسجيل الدخول)
```

الحزم المستخدمة: `provider` (الحالة)، `http` (الشبكة)، `flutter_secure_storage`
(التوكن)، `shared_preferences` (رابط الخادم)، `intl` (تنسيق العملة).

---

## 🚀 التشغيل والبناء للمتاجر

> يتطلب [Flutter SDK](https://docs.flutter.dev/get-started/install) (stable).

المستودع فيه كود Dart بس؛ قشرة أندرويد/iOS بتتولّد بأمر واحد (الاسم، الـ ID
`com.mousstec.mobile`، الصلاحيات، التوقيع، Firebase، الأيقونة وشاشة البداية):

```bash
cd mobile_app
flutter pub get
python3 tool/setup_native.py     # مرة واحدة (وآمن تتعاد)
flutter test                     # الاختبارات
flutter run                      # على جهاز/محاكي
```

### نسخة المتجر

```bash
flutter build appbundle --release   # Google Play (.aab)
flutter build ipa --release         # App Store (على ماك + Xcode)
```

**توقيع أندرويد:** اعمل keystore مرة واحدة واحفظه في مكان آمن (لو ضاع مش هتعرف تحدّث التطبيق):

```bash
keytool -genkey -v -keystore ~/mousstec-upload.jks -keyalg RSA -keysize 2048 -validity 10000 -alias upload
```

وبعدين `android/key.properties` (مش بيترفع على Git):

```properties
storePassword=...
keyPassword=...
keyAlias=upload
storeFile=/home/you/mousstec-upload.jks
```

الخطوات الكاملة للنشر (Play Console / App Store Connect، سياسة الخصوصية، الصور):
`docs/MOBILE_RELEASE.md`. والـ CI (`.github/workflows/mobile.yml`) بيحلل ويختبر ويبني APK
على أي تغيير في `mobile_app/`.

---

## 🔌 الربط بالخادم

1. في شاشة تسجيل الدخول اكتب **كود الشركة** (`myshop`) — أو الرابط كامل لو دومين خاص.
2. سجّل الدخول باسم مستخدم وكلمة مرور موظف الورشة (نفس بيانات لوحة تحكم الويب).
3. التطبيق يعزل البيانات تلقائياً حسب الورشة لأن الـ API يعمل داخل سياق الـ tenant.

يجب أن يسمح الخادم بنطاق الأصل (CORS) لطلبات الويب؛ على أندرويد/iOS الأصلية لا حاجة
لذلك. تأكّد أن `CORS_ALLOWED_ORIGINS` على الخادم مضبوط إن استُخدمت نسخة الويب.

نقاط النهاية المستهلكة (انظر تطبيق Django `mobile_api`):

| الغرض | الطريقة | المسار |
|------|---------|--------|
| تسجيل الدخول | POST | `/api/mobile/v1/auth/login/` |
| تحديث التوكن | POST | `/api/mobile/v1/auth/refresh/` |
| المستخدم الحالي | GET | `/api/mobile/v1/auth/me/` |
| لوحة المعلومات | GET | `/api/mobile/v1/dashboard/` |
| أوامر الشغل | GET | `/api/mobile/v1/work-orders/` |
| تفاصيل أمر شغل | GET | `/api/mobile/v1/work-orders/{id}/` |
| تغيير الحالة | POST | `/api/mobile/v1/work-orders/{id}/status/` |
| المخزون | GET | `/api/mobile/v1/products/` |
| نقص المخزون | GET | `/api/mobile/v1/products/low-stock/` |
| تفاصيل قطعة | GET | `/api/mobile/v1/products/{id}/` |
| العملاء | GET | `/api/mobile/v1/customers/` |
| 🔔 تسجيل/إلغاء جهاز الإشعارات | POST / DELETE | `/api/mobile/v1/devices/` |
| 🎨 لوحة المطبعة | GET | `/api/mobile/v1/print/dashboard/` |
| 🎨 الطلبات/الفواتير | GET / POST | `/api/mobile/v1/print/orders/` |
| 🎨 تأكيد/تسليم/إلغاء | POST | `/api/mobile/v1/print/orders/{id}/action/` |
| 🎨 لوحة الإنتاج + المرحلة الجاية | GET / POST | `/api/mobile/v1/print/jobs/` ، `/print/jobs/{id}/advance/` |
| 🎨 العملاء، الخامات، عروض الأسعار، البروفات، الماكينات | GET (+عملاء POST/PATCH) | `/api/mobile/v1/print/...` |

---

## ✅ حالة الاختبار

- `flutter test`: اختبارات وحدة وويدجت (تحليل JSON، عميل الـ API وتحديث التوكن، الدخول بكود
  الشركة، القطاع والصلاحيات، الترجمة الإنجليزي لكل الموديولات، شاشات المطابع).
- `flutter analyze` من غير أخطاء ولا تحذيرات.
- `python3 tool/setup_native.py && flutter build apk --release` اتجرّب وبيبني APK.
- السيرفر: `python manage.py test mobile_api` (منهم `mobile_api/test_printing.py` للمطابع والإشعارات).
