# 📱 نشر تطبيق Mouss Tec على Google Play و App Store

التطبيق واحد (Flutter) للورش والمطابع — بيتشكّل حسب نشاط الشركة بعد الدخول.
الكود في `mobile_app/`، والـ API في `mobile_api/`.

## 1) مرة واحدة: الحسابات

| | Google Play | App Store |
|---|---|---|
| الحساب | [Play Console](https://play.google.com/console) — 25$ مرة واحدة | [Apple Developer](https://developer.apple.com/programs/) — 99$ في السنة |
| الـ ID | `com.mousstec.mobile` | `com.mousstec.mobile` |
| البناء | أي جهاز (لينكس/ويندوز/ماك) | ماك + Xcode بس |

> ⚠️ Google Play للحسابات الشخصية الجديدة بيطلب **اختبار مغلق بـ 12 مختبِر لمدة 14 يوم** قبل النشر
> العام. حساب شركة (Organization) مش محتاج ده — محتاج D-U-N-S number.

## 2) مرة واحدة: Firebase (الإشعارات)

1. [Firebase Console](https://console.firebase.google.com) ← مشروع جديد «Mouss Tec».
2. ضيف تطبيق Android بـ `com.mousstec.mobile` ← نزّل `google-services.json` ← حطه في `mobile_app/firebase/`.
3. ضيف تطبيق iOS بـ `com.mousstec.mobile` ← نزّل `GoogleService-Info.plist` ← حطه في `mobile_app/firebase/`.
4. iOS بس: ارفع APNs Authentication Key (من Apple Developer ← Keys) في Firebase ← Cloud Messaging.
5. السيرفر: Project settings ← Service accounts ← Generate new private key ← حط المحتوى في
   `FCM_SERVICE_ACCOUNT_JSON` في `.env` وانشر (`bash deploy/deploy.sh`).

## 3) كل إصدار: البناء

```bash
cd mobile_app
# زوّد version في pubspec.yaml (مثال 1.1.0+2 ← 1.1.1+3 — الرقم بعد + لازم يزيد كل رفعة)
flutter pub get
python3 tool/setup_native.py
flutter test
flutter build appbundle --release      # ⇒ build/app/outputs/bundle/release/app-release.aab
flutter build ipa --release            # (ماك) ⇒ build/ios/ipa/  ثم ارفعه بـ Transporter أو Xcode
```

توقيع أندرويد: `android/key.properties` (الخطوات في `mobile_app/README.md`). **احتفظ بنسخة من الـ keystore
وكلمة السر برا الجهاز** — من غيرهم مش هتعرف تحدّث التطبيق على Play.

## 4) بيانات صفحة المتجر

- **الاسم:** Mouss Tec
- **وصف قصير (80 حرف):** إدارة ورشتك أو مطبعتك من موبايلك — طلبات، إنتاج، مخزون، فواتير.
- **سياسة الخصوصية:** `https://mousstec.com/privacy/` (موجودة في السيستم).
- **التصنيف:** Business — مش للأطفال.
- **Data safety (Play) / App Privacy (Apple):** الاسم والإيميل (حساب)، بيانات الشركة (طلبات/عملاء/مخزون)،
  توكن الإشعارات. مفيش إعلانات ولا تتبّع، والبيانات مشفّرة في النقل (HTTPS).
- **حساب تجربة للمراجعين (إجباري عند Apple):** اعمل شركة تجربة بكود ثابت (مثال `demo`) ومستخدم
  للمراجعة، واكتب الكود واسم المستخدم وكلمة السر في خانة Review notes.
- **الصور:** 2–8 سكرين شوت للموبايل (الرئيسية، الطلبات، لوحة الإنتاج، تفاصيل الطلب) بالعربي والإنجليزي.

## 5) قبل كل رفعة — تشيك ليست

- [ ] `flutter test` و `python manage.py test mobile_api` خُضر
- [ ] السيرفر متنشر بنفس نسخة الـ API (التطبيق القديم لازم يفضل شغال — مانمسحش مسارات)
- [ ] رقم الإصدار زاد في `pubspec.yaml`
- [ ] جرّبت الدخول بشركة ورشة وشركة مطبعة على جهاز حقيقي
