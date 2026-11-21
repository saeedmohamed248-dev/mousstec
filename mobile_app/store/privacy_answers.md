# إجابات أسئلة الخصوصية في المتجرين

مطابقة لسياسة الخصوصية (https://mousstec.com/privacy/) وللكود الفعلي للتطبيق.

## Google Play — Data safety

| السؤال | الإجابة |
|---|---|
| Does your app collect or share user data? | **Yes** |
| Is all data encrypted in transit? | **Yes** (HTTPS) |
| Do you provide a way to request data deletion? | **Yes** — من «مشكلة؟ كلّمنا» جوه البرنامج أو واتساب الدعم (مكتوب في سياسة الخصوصية) |
| Account creation | الحسابات بيعملها صاحب الشركة من الموقع — التطبيق فيه **تسجيل دخول بس** |

البيانات (كلها **Collected**، **مش Shared**، **مش اختيارية**، الغرض **App functionality** + **Account management**):

| النوع في الفورم | ليه |
|---|---|
| Personal info → Name, Email address, Phone number | حساب الموظف، وبيانات عملاء الشركة اللي بتظهر في التطبيق |
| Financial info → Purchase history / Other financial info | فواتير وطلبات ومدفوعات الشركة (بيانات شغل، مش كروت) |
| App activity → Other actions | تغيير حالات الطلبات ومراحل الإنتاج |
| Device or other IDs | توكن الإشعارات (Firebase) |

مفيش: موقع، صور/فيديو محفوظة (الكاميرا للباركود بس ومابتتحفظش)، جهات اتصال، إعلانات، تتبّع.

## Google Play — باقي أسئلة App content
- **Ads:** No
- **App access:** All or some functionality is restricted → اكتب حسابات المراجعة (تحت)
- **Target audience:** 18+
- **News app:** No — **COVID tracing:** No — **Government app:** No
- **Financial features:** None (الدفع على الموقع، مش جوه التطبيق)

## App Store — App Privacy

Data used to track you: **None**. Data linked to you (App Functionality):
- Contact Info → Name, Email Address, Phone Number
- Financial Info → Other Financial Info
- Identifiers → User ID, Device ID (توكن الإشعارات)
- Usage Data → Product Interaction

## App Store — أسئلة تانية
- **Export compliance (Encryption):** التطبيق بيستخدم HTTPS العادي بس ⇒ مُعفى
  (`ITSAppUsesNonExemptEncryption = NO` متحطّة في Info.plist بالفعل — مش هيسألك كل رفعة).
- **Sign in with Apple:** مش مطلوب — الدخول بحساب الشركة (مش Google/Facebook).
- **حذف الحساب جوه التطبيق:** مش مطلوب — التطبيق مابيعملش حسابات.
