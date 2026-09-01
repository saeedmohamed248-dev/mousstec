# 🌍 دليل التوطين (Localization) — من ERP مصري إلى منصة عالمية

> آخر تحديث: أغسطس ٢٠٢٦ · الحالة: **المرحلة ١ (النواة) مكتملة**

النظام كان مقفولاً تشغيلياً على مصر: **٤٨٨ موضع** يعرض `ج.م` ثابتة،
توقيت `Africa/Cairo`، ولغة `ar` فقط. هذا الدليل يشرح النواة التي فكّت
القفل، وكيفية إضافة دولة (مثل الإمارات)، وخطة ترحيل العرض القديم.

---

## 1. النواة (Single Source of Truth)

كل ما يخص العملة/الضريبة/التوقيت/اللغة مصدره ملف واحد:

**`erp_core/localization.py`**

| الدالة | الوظيفة |
|--------|---------|
| `COUNTRY_CONFIG` | إعدادات كل دولة (currency, vat_rate, timezone, language, flag) |
| `format_money(amount, currency, lang)` | `1,234.50 د.إ` — يحترم خانات العملة (الدينار ٣) |
| `currency_symbol(currency, lang)` | رمز العملة بالعربي/الإنجليزي |
| `currency_for_country(country)` / `vat_rate_for_country(country)` | اشتقاق |
| `resolve_tenant_localization(tenant)` | يرجّع إعدادات المستأجر الفعّالة (يسقط بأمان للافتراضي) |

الدول المدعومة حالياً: 🇪🇬 مصر · 🇦🇪 الإمارات · 🇸🇦 السعودية · 🇶🇦 قطر ·
🇰🇼 الكويت · 🇴🇲 عُمان · 🇧🇭 البحرين · 🇺🇸 أمريكا · 🇬🇧 بريطانيا.
لإضافة دولة: أضف سطراً واحداً في `COUNTRY_CONFIG` (والعملة في `CURRENCY_META` إن جديدة).

---

## 2. حقول المستأجر الجديدة (`clients.Client`)

migration: `clients/migrations/0074_tenant_localization.py` — **additive وآمن**
(كل الصفوف الحالية تبقى `country='EG'` ⇒ سلوك مطابق للسابق تماماً).

```python
country          = 'EG'   # يقود الباقي تلقائياً
currency         = ''      # فارغ ⇒ يُشتق من الدولة
vat_rate         = None    # فارغ ⇒ النسبة القانونية للدولة
timezone         = ''      # فارغ ⇒ توقيت الدولة
default_language = ''      # فارغ ⇒ لغة الدولة
```

خصائص جاهزة على الموديل: `tenant.localization` (dict)، `tenant.effective_currency`،
`tenant.effective_vat_rate`، `tenant.effective_timezone`.

### مثال: تفعيل مستأجر إماراتي
```python
tenant.country = 'AE'
tenant.save()
# النتيجة تلقائياً: عملة د.إ (AED) · ضريبة 5% · توقيت Asia/Dubai
```

---

## 3. العرض في القوالب (Templates)

context processor `erp_core.context_processors.tenant_context` يحقن في **كل**
قالب:

- `tenant_currency` — كود العملة (مثل `AED`)
- `tenant_currency_symbol` — الرمز (مثل `د.إ`)
- `tenant_vat_rate` — نسبة الضريبة
- `tenant_localization` — الـ dict الكامل

وفلتر جاهز:

```django
{% load money_tags %}

{{ invoice.total|money }}          {# 1,000.00 د.إ — عملة المستأجر تلقائياً #}
{{ amount|money:"USD" }}           {# عملة صريحة #}
{{ amount|money_en }}              {# رمز إنجليزي للفواتير الدولية #}
{{ amount|money_plain }}           {# رقم فقط بدون رمز #}
```

---

## 4. العرض في Python (admin / services / reports)

```python
from erp_core.localization import format_money, resolve_tenant_localization
from django.db import connection

cur = resolve_tenant_localization(connection.tenant)['currency']
label = format_money(obj.amount, currency=cur)   # بدل f"{x} ج.م"
```

---

## 5. خطة ترحيل الـ ٤٨٨ موضعاً (المرحلة ٢ — تدريجية)

النواة **لا تكسر** أي شيء قائم — `ج.م` الثابتة تظل تعمل حتى تُستبدل. الترحيل
يتم على دفعات مختبرة، بالأولوية:

1. **القوالب المواجهة للعميل** (marketplace / manage_subscription / الفواتير) —
   استبدل `ج.م` بـ `{{ x|money }}`. أعلى أثر تجاري.
2. **admin display helpers** (`inventory/admin/*.py`) — استبدل
   `format_html('... ج.م', v)` بـ `format_money(v, currency=...)`.
3. **reports/services** (`inventory/services/reporting_service.py`) — استخدم
   `format_money`.

> ⚠️ **لا تُحوّل دفعة كبيرة بدون تشغيل test suite.** بعض `ج.م` بجانب أرقام
> يرسمها JavaScript — تحتاج تمرير `tenant_currency_symbol` للـ JS context.

### للعثور على المتبقي
```bash
grep -rn "ج\.م" --include=*.html --include=*.py . | wc -l
```

---

## 6. ما تبقّى للعالمية الكاملة (Roadmap)

- [x] نواة عملة/ضريبة/توقيت + حقول المستأجر + فلتر + context (المرحلة ١)
- [ ] ترحيل الـ ٤٨٨ موضع عرض (المرحلة ٢)
- [ ] بوابة دفع إماراتية (Telr / Network International / Stripe) بجانب Paymob
- [ ] فواتير متوافقة مع الهيئة الاتحادية للضرائب (FTA) — رقم ضريبي + QR
- [ ] ترجمة إنجليزية فعلية (`locale/en/LC_MESSAGES/django.po` غير موجود حالياً)
- [ ] `activate_timezone` middleware يفعّل توقيت المستأجر لكل request
- [ ] onboarding يسأل عن الدولة ويضبط الباقي تلقائياً

---

## 7. الاختبارات

`erp_core/tests/test_localization.py` — ٢٣ اختباراً (SimpleTestCase، بلا DB):
تنسيق العملات، اشتقاق الدولة، احترام ضريبة الصفر الصريحة، والسقوط الآمن.

```bash
python manage.py test erp_core.tests.test_localization
```
