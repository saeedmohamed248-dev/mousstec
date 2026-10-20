# Mouss Tec

منصة SaaS لإدارة مراكز صيانة السيارات وتجار قطع الغيار والمطابع: مخزن ونقطة بيع
وفواتير ومشتريات وخزائن ودفاتر محاسبية، وورشة وكروت صيانة، وموارد بشرية ومرتبات،
وسوق B2B/C2C، وتشخيص ذكي وروبوت ورشة. كل شركة ليها قاعدة بيانات معزولة
(PostgreSQL schema) على دومين فرعي خاص بيها.

**التقنيات:** Django 4.2 + django-tenants + PostgreSQL 16 + Redis 7 + Celery + Channels
(daphne)، والواجهة قوالب Django + Tailwind/Alpine، عربي (RTL) وإنجليزي (LTR).

---

## ⚡ التشغيل محلياً (١٠ دقايق)

المطلوب: Python **3.12** (مش 3.13 — `numpy==1.26.4` مالوش wheel ليه)، PostgreSQL 16،
Redis 7، و Node 22 (لاختبارات المتصفح بس).

```bash
python3.12 -m venv venv && source venv/bin/activate
make setup                      # pip install + Playwright

# .env (manage.py بينسخ .env.example لو مش موجود) — أقل حاجة للتطوير:
#   DEBUG=True
#   SECRET_KEY=any-local-dev-key
#   BASE_DOMAIN=localhost
#   DATABASE_URL=postgres://postgres:123@localhost:5432/erp_db
#   REDIS_URL=redis://127.0.0.1:6379/1
#   ENABLE_AI_PREDICTIONS=False

make services                   # Postgres + Redis (أو شغّلهم بطريقتك)
make migrate                    # ⚠️ migrate العادي مقفول — المشروع multi-tenant
make seed                       # شركة تجريبية ببيانات
make run
```

افتح **http://demo.localhost:8000/login/** وادخل بـ `admin` / `Demo12345`
(وفيه كمان `cashier` و`tech` و`accountant` بنفس كلمة السر). المتصفح بيحوّل أي
`*.localhost` لجهازك لوحده.

`make help` بيعرض كل الأوامر.

---

## 🗺️ خريطة المشروع

| الجزء | بيعمل إيه | المسار |
|---|---|---|
| `inventory` | **قلب الـ ERP**: أصناف ومخزن، نقطة البيع، فواتير البيع والشراء، عملاء وموردين، خزائن، مصروفات، كروت صيانة، تقارير ودفاتر محاسبية | `/system/` |
| `clients` | المنصة نفسها: الشركات (tenants)، التسجيل والدخول، الباقات والاشتراكات والدفع، لوحة المالك، الماركت بليس | `/`, `/connect/`, `/pricing/`, `/superadmin/` |
| `tenancy` | الشركات والصلاحيات حسب الباقة، حدود الاستخدام، **قالب تأسيس الشركات الجديدة** | — |
| `billing` | Paymob (الدفع/التوقيع/الاسترداد) | `/payment/` |
| `hr` | الحضور والانصراف (**مصدر واحد للمرتبات**: صفحة الحضور + زراير الورشة/شاشة الفني بتكتب في `AttendanceRecord`)، السلف، الإجازات، المرتبات | `/hr/` |
| `workshop` | منطق الورشة والصيانة الوقائية | — |
| `printing` | نسخة المطابع (طلبات الطباعة، المصمم، الخزينة) | `/printing/` |
| `marketplace_b2b` / `marketplace_c2c` | سوق التجار (مزايدات، Escrow) وسوق العملاء | `/api/v1/b2b/`, `/marketplace/` |
| `smart_diagnostics` / `bmw_ecu` / `repair_atlas` / `diagnostics_catalog` | تشخيص OBD وBMW وأطلس الإصلاح | `/api/diagnostics/`, `/repair-atlas/` |
| `robot` / `smart_robot` | روبوت الورشة (Django) وكشك قطع الغيار (FastAPI منفصل) | `/robot/`, `/api/robot/v1/` |
| `omnichannel` / `messenger_bot` / `social_ads` | واتساب/ماسنجر والتسويق الآلي | — |
| `mobile_api` | API تطبيق الموبايل | `/api/mobile/v1/` |
| `erp_core` | الإعدادات، الـ URLs، الـ middleware، أدوات الذكاء الاصطناعي المشتركة | — |

التفاصيل: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) · الدورة المحاسبية:
[docs/ACCOUNTING_CYCLE.md](docs/ACCOUNTING_CYCLE.md) · الترجمة: [docs/I18N.md](docs/I18N.md)

---

## 🧪 الاختبارات

| | الأمر | ملاحظات |
|---|---|---|
| تطبيق واحد | `make test-app APP=hr` | الأسرع وانت شغّال |
| زي CI | `make test-shard SHARD=1` (١ لـ ٤) | `scripts/test_shards.py` بيقسّم السويت |
| كلها | `make test` | ~٢٣٠٠ اختبار — محتاج Redis وإلا كل كلاس بيستنى دقيقة |
| المتصفح | `make e2e-server` ثم `make e2e` | عربي + إنجليزي — [e2e/README.md](e2e/README.md) |
| لينت | `make lint` و `make check` | نفس اللي في CI |

كل PR على `main` بيشغّل CI كامل (`.github/workflows/ci.yml`): لينت، مهاجرات، السويت
كاملة على ٤ أجهزة، اختبارات المتصفح، واختبارات الكشك. **ماحدش ينشر غير لو CI أخضر.**

---

## 🚀 النشر

يدوي على DigitalOcean (Docker Compose) — الخطوات في
[docs/DEPLOY_RUNBOOK.md](docs/DEPLOY_RUNBOOK.md). الـ `--build` إجباري.

## 🤝 المساهمة

اقرا [CONTRIBUTING.md](CONTRIBUTING.md) قبل أول PR — فيه قواعد الفلوس والترجمة والـ
multi-tenant اللي لو اتكسرت بتوصل للعميل.
