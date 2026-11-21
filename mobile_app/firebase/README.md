# 🔔 ملفات Firebase (للإشعارات)

حط هنا الملفين اللي بتنزّلهم من [Firebase Console](https://console.firebase.google.com)
(Project settings ← Your apps):

| الملف | المنصّة | الـ ID اللي تسجّل بيه التطبيق |
|------|---------|------------------------------|
| `google-services.json` | أندرويد | `com.mousstec.mobile` |
| `GoogleService-Info.plist` | iOS | `com.mousstec.mobile` |

بعدها شغّل `python3 tool/setup_native.py` وهو بينسخهم مكانهم.

وعلى السيرفر: من Firebase ← Project settings ← Service accounts ← Generate new private key،
وحط محتوى الملف في متغيّر البيئة `FCM_SERVICE_ACCOUNT_JSON` (أو مساره في `FCM_SERVICE_ACCOUNT_FILE`).

من غير الملفات دي التطبيق شغال عادي — الإشعارات بس هي اللي مقفولة.
