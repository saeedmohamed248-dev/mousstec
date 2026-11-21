# ملاحظات المراجعة (App Review Information / App access)

قبل أول رفعة شغّل على السيرفر (مرة واحدة):

```bash
docker compose exec web python manage.py store_review_demo --password "اختار كلمة سر قوية"
```

وبعدين انسخ النص ده في **App Store Connect ← App Review Information ← Notes** وفي
**Play Console ← App content ← App access** (حط كلمة السر اللي اخترتها مكان `<PASSWORD>`):

```
Mouss Tec is a B2B app for subscribers of the Mouss Tec management system
(auto workshops and print shops). Accounts are created by the company owner on
mousstec.com — there is no sign-up inside the app.

Two demo companies are ready (the app adapts to the company's business type):

1) Print shop
   Company code: appreviewprint
   Username:     reviewer
   Password:     <PASSWORD>
   Try: Orders tab → open an order → "Delivered"; Production tab → move a job
   to the next stage; the "burger boxes" job is blocked until the customer
   approves its proof.

2) Auto workshop
   Company code: appreview
   Username:     reviewer
   Password:     <PASSWORD>
   Try: Work orders, Inventory (barcode scan), More → all modules.

Language can be switched to English from the login screen (top corner) or
Settings. Push notifications are used only for work events (proof approved,
low stock, leave request).
```

وفي آبل كمان: **Sign-in required = Yes**، والاسم/الموبايل/الإيميل بتوعك كجهة تواصل.
