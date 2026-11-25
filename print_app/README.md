# Mousstec Print — تطبيق سوق المطابع

تطبيق أندرويد خفيف (Capacitor) بيفتح **سوق المطابع** على
`https://mousstec.com/marketplace/catalog/printing/` — للشركات والعملاء: كتالوج، طلب عرض سعر، متابعة الطلبات والبروفات.

- **أي تعديل على الموقع بيظهر في التطبيق فوراً** — من غير رفع نسخة جديدة.
- نسخة جديدة على المتجر مطلوبة بس لو اتغيّر حاجة في التطبيق نفسه (الأيقونة، الاسم، الصلاحيات).
- البناء: GitHub Actions ← **Print App** ← Run workflow ← نزّل `mousstec-print-aab` من Artifacts.
- التوقيع: نفس مفتاح Mousstec (`ANDROID_KEYSTORE_*` في Secrets).
- اسم الحزمة: `com.mousstec.print`
