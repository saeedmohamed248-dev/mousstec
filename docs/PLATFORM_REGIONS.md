# 🌐 نسختا المنصة — موقع مصري + موقع إماراتي

> نفس الكود، نفس السيرفر، دومينين. كل دومين يمثّل دولة بعملتها وأسعارها.

| الدومين | الدولة | العملة | الأسعار |
|---------|--------|--------|---------|
| `mousstec.com` | 🇪🇬 مصر | ج.م (EGP) | `Plan.monthly_price` |
| `ae.mousstec.com` | 🇦🇪 الإمارات | د.إ (AED) | `Plan.monthly_price_aed` |

الزائر على الموقع الإماراتي يشوف كل الأسعار بالدرهم، وأي تسجيل من هناك
يطلع **حساب إماراتي تلقائياً** (AED + ضريبة 5٪ + توقيت دبي).

---

## كيف يعمل (Code)

- `erp_core/regions.py` — يشتق المنطقة من host الطلب (لا middleware، لا tenant إضافي).
- `context_processors.py` — على الـ public schema، عملة العرض = عملة المنطقة،
  فكل صفحات التسويق (اللي تستخدم `tenant_currency_symbol`) تعرض العملة الصح.
- `subscription_views.saas_pricing_page` — يعرض `monthly_price_aed` على الموقع
  الإماراتي (fallback للسعر المصري لو AED = 0).
- `auth_views.register_new_tenant_saas` — المستأجر الجديد يأخذ `country` = دولة المنطقة.
- إعداد: `REGION_AE_HOSTS` + `DEFAULT_REGION_COUNTRY` في settings/env.

---

## خطوات التفعيل على السيرفر (مرة واحدة)

### 1) DNS — وجّه الساب دومين للسيرفر
لو عندك سجل `*.mousstec.com` wildcard A-record → مغطّى تلقائياً. وإلا أضِف:
```
A    ae.mousstec.com    →    139.59.156.252
```
(شهادة TLS: Caddy يغطّيها ضمن `*.mousstec.com` — لا شيء إضافي.)

### 2) اربط الدومين بالـ public schema (عشان django-tenants يخدمه)
```bash
cd ~/mousstec
docker compose exec web python manage.py shell -c "
from clients.models import Client, Domain
public = Client.objects.get(schema_name='public')
d, created = Domain.objects.get_or_create(domain='ae.mousstec.com', tenant=public, defaults={'is_primary': False})
print('domain', d.domain, 'created' if created else 'exists', '→', public.schema_name)
"
```

### 3) اضبط أسعار الدرهم للباقات
من لوحة السوبر أدمن: **الباقات → كل باقة → التسعير → «السعر الشهري (د.إ)»**.
أو دفعة واحدة عبر shell (مثال):
```bash
docker compose exec web python manage.py shell -c "
from clients.models import Plan
prices = {'silver': 149, 'gold': 349, 'empire': 799,
          'print_basic': 199, 'print_pro': 449, 'print_enterprise': 899}
for slug, aed in prices.items():
    Plan.objects.filter(slug=slug).update(monthly_price_aed=aed)
print('AED prices set for', len(prices), 'plans')
"
```
> عدّل الأرقام حسب تسعيرك. أي باقة سعرها AED = 0 هتعرض السعر المصري.

### 4) (اختياري) متغيرات البيئة
افتراضياً `ae.mousstec.com` = إمارات. لتغيير/إضافة هوستات، في `.env`:
```
REGION_AE_HOSTS=ae.mousstec.com,mousstec.ae
DEFAULT_REGION_COUNTRY=EG
```

---

## التأكيد بعد التفعيل
1. افتح **`https://mousstec.com/pricing/`** → أسعار بالجنيه (ج.م).
2. افتح **`https://ae.mousstec.com/pricing/`** → نفس الباقات بالدرهم (د.إ).
3. سجّل حساب تجريبي من الموقع الإماراتي → لوحته تطلع AED تلقائياً.

---

## ملاحظات
- الـ `ae` محجوز كمنطقة — لا تُنشئ مستأجر subdomain اسمه `ae`.
- سطر «إضافة موظف/فرع 125» في صفحة الأسعار قيمته ثابتة بالجنيه؛ لو حابب سعر
  درهم منفصل له، نضيفه لاحقاً (نفس نمط `monthly_price_aed`).
- بوابة الدفع الإماراتية (Telr/Stripe) + ترجمة إنجليزية = خطوات لاحقة مستقلة.
