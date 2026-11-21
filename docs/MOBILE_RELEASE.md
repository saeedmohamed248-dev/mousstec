# 📱 نشر تطبيق Mouss Tec على Google Play و App Store

التطبيق واحد (Flutter) للورش والمطابع — بيتشكّل حسب نشاط الشركة بعد الدخول.
الكود في `mobile_app/`، والـ API في `mobile_api/`، والرفع أوتوماتيك من GitHub
(`.github/workflows/mobile-release.yml`) — **مش محتاج ماك ولا Android Studio**.

| الملف | فيه إيه |
|---|---|
| `mobile_app/store/listing.ar.md` و `listing.en.md` | كل نصوص صفحة المتجر (الاسم، الوصف، الكلمات المفتاحية…) جاهزة للنسخ |
| `mobile_app/store/privacy_answers.md` | إجابات Data safety (جوجل) و App Privacy (آبل) |
| `mobile_app/store/review_notes.md` | حسابات التجربة للمراجعين + النص اللي بيتكتب لهم |

---

## الخطوة 1 — الحسابات (إنت — مرة واحدة)

| | Google Play | App Store |
|---|---|---|
| التسجيل | [play.google.com/console](https://play.google.com/console) — 25$ مرة واحدة | [developer.apple.com/programs](https://developer.apple.com/programs/) — 99$ في السنة |
| النوع | **Organization** (باسم الشركة — يعفيك من اختبار الـ 12 مختبِر × 14 يوم) | **Organization** |
| محتاج | السجل التجاري + رقم D-U-N-S بنفس الاسم والعنوان بالظبط | نفس رقم D-U-N-S |
| الـ ID | `com.mousstec.mobile` | `com.mousstec.mobile` |

> رقم D-U-N-S مجاني، وبتطلبه من [صفحة آبل](https://developer.apple.com/enroll/duns-lookup/) — ممكن ياخد
> من يومين لأسبوعين، فاطلبه **أول حاجة**.

## الخطوة 2 — مفتاح توقيع أندرويد (إنت — مرة واحدة، 2 دقيقة)

على أي جهاز عليه Java (أو Android Studio):

```bash
keytool -genkey -v -keystore mousstec-upload.jks -keyalg RSA -keysize 2048 -validity 10000 -alias upload
base64 -w0 mousstec-upload.jks > keystore.txt      # على ماك: base64 -i mousstec-upload.jks -o keystore.txt
```

⚠️ **احفظ ملف `mousstec-upload.jks` وكلمة السر في مكانين آمنين** (مثلاً Google Drive + فلاشة).
لو ضاعوا هتحتاج تكلّم دعم جوجل عشان تحدّث التطبيق.

## الخطوة 3 — مفاتيح الرفع (إنت — مرة واحدة)

**Google Play — Service account:**
1. [Google Cloud Console](https://console.cloud.google.com) ← مشروع جديد ← فعّل **Google Play Android Developer API**.
2. IAM ← Service Accounts ← Create ← بعدين Keys ← Add key ← JSON (بينزل ملف).
3. Play Console ← Users and permissions ← Invite new users ← إيميل الـ service account ←
   صلاحية **Release apps to testing tracks** و **Release to production** على التطبيق.

**App Store — API key:**
1. App Store Connect ← Users and Access ← Integrations ← App Store Connect API ← **+**
   ← الاسم «GitHub» والصلاحية **Admin** (عشان يعمل شهادات التوقيع لوحده).
2. انزّل ملف `AuthKey_XXXX.p8` (بينزل **مرة واحدة بس**) واكتب الـ Key ID والـ Issuer ID.
3. Team ID: من [developer.apple.com/account](https://developer.apple.com/account) ← Membership details.

## الخطوة 4 — حط المفاتيح في GitHub (إنت — مرة واحدة)

GitHub ← المستودع ← Settings ← Secrets and variables ← Actions ← **New repository secret**:

| الاسم | القيمة |
|---|---|
| `ANDROID_KEYSTORE_BASE64` | محتوى `keystore.txt` |
| `ANDROID_KEYSTORE_PASSWORD` | كلمة سر الـ keystore |
| `ANDROID_KEY_ALIAS` | `upload` |
| `ANDROID_KEY_PASSWORD` | كلمة سر المفتاح (غالباً نفس اللي فوق) |
| `PLAY_SERVICE_ACCOUNT_JSON` | محتوى ملف الـ JSON بتاع الـ service account |
| `APPLE_TEAM_ID` | الـ Team ID |
| `ASC_KEY_ID` | الـ Key ID |
| `ASC_ISSUER_ID` | الـ Issuer ID |
| `ASC_PRIVATE_KEY` | محتوى ملف `AuthKey_XXXX.p8` كله |
| `FIREBASE_GOOGLE_SERVICES_JSON` | (للإشعارات — اختياري) محتوى `google-services.json` |
| `FIREBASE_GOOGLE_SERVICE_INFO_PLIST` | (للإشعارات — اختياري) محتوى `GoogleService-Info.plist` |

المفاتيح دي مش بتتشاف تاني بعد ما تتحفظ، ومحدش يقدر يقراها من الكود.

## الخطوة 5 — Firebase للإشعارات (اختياري — ممكن بعدين)

1. [console.firebase.google.com](https://console.firebase.google.com) ← مشروع «Mouss Tec».
2. Add app ← Android ← `com.mousstec.mobile` ← نزّل `google-services.json` ← secret فوق.
3. Add app ← iOS ← `com.mousstec.mobile` ← نزّل `GoogleService-Info.plist` ← secret فوق.
4. Project settings ← Cloud Messaging ← Apple app configuration ← ارفع **APNs Authentication Key**
   (من developer.apple.com ← Keys ← + ← Apple Push Notifications service).
5. Project settings ← Service accounts ← Generate new private key ← محتواه في `.env` على السيرفر
   باسم `FCM_SERVICE_ACCOUNT_JSON` وبعدين `bash deploy/deploy.sh`.

## الخطوة 6 — حسابات المراجعين (مرة واحدة على السيرفر)

```bash
docker compose exec web python manage.py store_review_demo --password "كلمة سر قوية"
```

بيعمل شركتين تجربة: `appreview` (ورشة) و `appreviewprint` (مطبعة)، المستخدم `reviewer`.
النص اللي بيتكتب للمراجع في `mobile_app/store/review_notes.md`.

## الخطوة 7 — أول رفعة

**App Store:**
1. developer.apple.com ← Certificates, IDs & Profiles ← Identifiers ← **+** ← App IDs ←
   `com.mousstec.mobile` وعلّم **Push Notifications**.
2. App Store Connect ← Apps ← **+** New App ← iOS، الاسم Mouss Tec، الـ Bundle ID اللي فوق، SKU: `mousstec-mobile`.
3. GitHub ← Actions ← **Mobile Release** ← Run workflow ← platform: `ios`.
   بعد ~20 دقيقة النسخة بتظهر في TestFlight.
4. املا صفحة المتجر من `store/listing.*.md` و App Privacy من `store/privacy_answers.md`،
   والصور، وملاحظات المراجعة ← **Submit for Review**.

**Google Play:**
1. Play Console ← Create app ← الاسم Mouss Tec، App، Free.
2. GitHub ← Actions ← **Mobile Release** ← Run workflow ← platform: `android`.
   **أول مرة بس** جوجل بيرفض الرفع من الـ API قبل أول رفعة يدوي: لو وقع، نزّل ملف الـ `.aab`
   من نفس التشغيل (Artifacts) وارفعه بإيدك في Testing ← Internal testing ← Create new release.
3. املا App content (Data safety، Ads، Target audience، App access) من `store/privacy_answers.md`
   و `store/review_notes.md`، وصفحة المتجر من `store/listing.*.md`.
4. Production ← Create new release ← اختار النسخة ← **Start rollout to Production**.

## كل تحديث بعد كده

GitHub ← Actions ← **Mobile Release** ← Run workflow (المنصّتين). رقم البناء بيزيد لوحده.
الرفع على جوجل بيبقى **مسودة** — بتدخل Play Console وتدوس «نشر». وفي آبل تختار النسخة وتبعتها للمراجعة.
لو غيّرت رقم الإصدار نفسه (1.1.0 ← 1.2.0) غيّره في `mobile_app/pubspec.yaml` وفي `settings_screen.dart`.

## الصور (Screenshots)

- **جوجل:** من 2 لـ 8 صور موبايل (أقل حاجة 1080 بكسل).
- **آبل:** صور آيفون 6.9 بوصة (1320×2868) — آبل بتصغّرها لباقي المقاسات.
- أسهل طريقة: نزّل النسخة التجريبية (Internal testing / TestFlight) على موبايلك، ادخل بشركة
  `appreviewprint`، وصوّر: الرئيسية، الطلبات، تفاصيل طلب، لوحة الإنتاج، فاتورة جديدة — مرة عربي ومرة إنجليزي.

## قبل كل رفعة — تشيك ليست

- [ ] الـ CI أخضر (`Mobile` و `CI`)
- [ ] السيرفر متنشر بنفس نسخة الـ API (التطبيق القديم لازم يفضل شغال — مانمسحش مسارات)
- [ ] جرّبت الدخول بشركة ورشة وشركة مطبعة على جهاز حقيقي
