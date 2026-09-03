# 📱 Mouss Tec Mobile (Flutter — Android & iOS)

تطبيق موبايل واحد بكود Flutter مشترك يعمل على **أندرويد** و **iOS**، يتصل بنظام
Mouss Tec ERP عبر واجهة **Mobile API** الجديدة (`/api/mobile/v1/`).

يغطّي هذا الإصدار (v1.0.0):

- 🔐 **تسجيل الدخول** عبر JWT مع تخزين آمن للتوكن (Keychain / Keystore) وتحديث تلقائي.
- 📊 **لوحة معلومات** بمؤشرات الورشة والمخزون وإيراد اليوم.
- 🔧 **أوامر شغل الصيانة**: قائمة + بحث + فلترة بالحالة + تفاصيل + **تغيير حالة أمر الشغل**
  (عرض سعر → قيد العمل → فحص الجودة → جاهز → تم التسليم).
- 📦 **المخزون وقطع الغيار**: قائمة + بحث + تنبيهات نقص المخزون + تفاصيل القطعة
  وتوزيعها على الفروع.
- ⚙️ **إعدادات**: ضبط رابط الورشة (النطاق الفرعي للـ tenant) + تسجيل الخروج.
- 🌐 واجهة عربية بالكامل مع دعم RTL.

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

## 🚀 التشغيل من الصفر

> يتطلب [Flutter SDK](https://docs.flutter.dev/get-started/install) 3.10+ مثبتاً.

هذا المستودع يلتزم بكود Dart (`lib/`, `test/`, `pubspec.yaml`) فقط دون القشرة
الأصلية للمنصّات (مجلدات `android/` و `ios/`) لإبقائه نظيفاً. تُولَّد القشرة محلياً
مرة واحدة:

```bash
cd mobile_app

# 1) توليد قشرة أندرويد و iOS (لن يمسّ lib/ ولا pubspec.yaml)
flutter create . --org com.mousstec --platforms=android,ios

# 2) تنزيل الحزم
flutter pub get

# 3) تشغيل الاختبارات
flutter test

# 4) تحليل الكود (lint)
flutter analyze

# 5) التشغيل على جهاز/محاكي
flutter run
```

### البناء للإنتاج

```bash
flutter build apk --release          # أندرويد (APK)
flutter build appbundle --release    # أندرويد (Play Store)
flutter build ios --release          # iOS (يتطلب macOS + Xcode)
```

---

## 🔌 الربط بالخادم

1. في شاشة تسجيل الدخول، افتح **«إعدادات الخادم»** وأدخل رابط ورشتك، مثل:
   `https://myshop.mousstec.com` (النطاق الفرعي الخاص بالـ tenant).
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

---

## ✅ حالة الاختبار

- **اختبارات الوحدة/الويدجت** مكتوبة في `test/` (تحليل JSON، عميل الـ API مع تحديث
  التوكن، سير تسجيل الدخول). شغّلها بـ `flutter test`.
- لم يتم بناء القشرة الأصلية أو تشغيلها على جهاز داخل بيئة التطوير هذه (لا يتوفّر بها
  Flutter SDK)؛ اتبع خطوات «التشغيل من الصفر» أعلاه على جهاز به الـ SDK.
