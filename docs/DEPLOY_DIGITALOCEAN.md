# 🚀 نشر Mouss Tec على DigitalOcean — دليل خطوة بخطوة

الدليل ده بيشغّل **كل حاجة على سيرفر واحد (Droplet)** باستخدام Docker:
Postgres + Redis + الويب (daphne) + Celery (worker + beat) + Caddy (HTTPS تلقائي).
مش محتاج تظبط أي حاجة بإيدك — كله في `docker-compose.yml`.

---

## 📋 اللي هتحتاجه
- حساب DigitalOcean (عندك ✅).
- دومين (عندك واحد ✅) — هتوجّهه للسيرفر.
- ٢٠–٣٠ دقيقة.
- التكلفة: Droplet بـ **$12–24/شهر** (رام 2–4 جيجا).

---

## الخطوة 1️⃣ — اعمل Droplet
1. من DigitalOcean اضغط **Create → Droplets**.
2. **Region:** Frankfurt (أقرب لمصر).
3. **Image:** Ubuntu 24.04 LTS.
4. **Droplet type:** Basic → Regular → **2 GB / 2 CPU** (أو 4GB لو الشغل كبير).
5. **Authentication:** SSH Key (أفضل) أو Password.
6. سمّيه `mousstec-prod` واضغط **Create**.
7. بعد دقيقة هيظهر **الرقم (IP)** بتاع السيرفر — انسخه (شكله زي `165.22.x.x`).

## الخطوة 2️⃣ — وجّه الدومين للسيرفر
من مكان تسجيل الدومين (أو DigitalOcean → Networking → Domains):
- اعمل سجل **A** لـ `@`  → يشاور على IP السيرفر.
- اعمل سجل **A** لـ `*` (wildcard) → نفس IP (عشان سَبدومينات الفروع).
- (اختياري) سجل **A** لـ `www` → نفس IP.

> ملاحظة: انتشار الـ DNS ممكن ياخد من دقايق لساعات.

## الخطوة 3️⃣ — ادخل السيرفر وثبّت Docker
افتح Terminal على جهازك:
```
ssh root@IP-السيرفر
```
وبعد ما تدخل، ثبّت Docker بأمر واحد:
```
curl -fsSL https://get.docker.com | sh
```

## الخطوة 4️⃣ — نزّل الكود
```
git clone https://github.com/saeedmohamed248-dev/mousstec.git
cd mousstec
```
> بعد دمج الـ PR اشتغل على `main`. للتجربة قبل الدمج:
> `git checkout claude/spare-parts-return-verification-4a3eqk`

## الخطوة 5️⃣ — اظبط ملف البيئة `.env`
```
cp .env.production.example .env
nano .env
```
عدّل على الأقل دي:
- `SECRET_KEY` → ولّد واحد: `python3 -c "import secrets;print(secrets.token_urlsafe(64))"`
- `BASE_DOMAIN` → دومينك (مثال: `mousstec.com`)
- `ACME_EMAIL` → إيميلك
- `POSTGRES_PASSWORD` **و** نفس الكلمة داخل `DATABASE_URL`
احفظ بـ `Ctrl+O` ثم `Enter` ثم اخرج بـ `Ctrl+X`.

## الخطوة 6️⃣ — شغّل كل حاجة 🚀
```
docker compose --env-file .env up -d --build
```
أول مرة هتاخد شوية (بيبني الصورة). تابع اللوجز:
```
docker compose logs -f web
```
لما تشوف **"تشغيل خادم ASGI (daphne)"** يبقى تمام — اضغط `Ctrl+C` للخروج من اللوجز (الخدمة بتفضل شغّالة).

## الخطوة 7️⃣ — اعمل أول فرع (tenant) + مدير
لسه قاعدة البيانات فاضية، فلازم نعمل الفرع الأول. ادخل شل Django:
```
docker compose exec web python manage.py shell
```
والصق ده (غيّر الدومين والباسورد):
```python
from django_tenants.utils import schema_context
from clients.models import Client, Domain
from django.contrib.auth.models import User

BASE = "mousstec.com"          # نفس BASE_DOMAIN
SUB  = "demo"                  # فرعك: demo.mousstec.com

# 1) سجل عام (public) لازم يتعمل مرة واحدة
if not Client.objects.filter(schema_name="public").exists():
    pub = Client(schema_name="public", name="Public")
    pub.save()
    Domain.objects.get_or_create(domain=BASE, tenant=pub, defaults={"is_primary": True})

# 2) فرعك الأول
t = Client(schema_name=SUB.replace("-", "_"), name="فرعي الأول")
t.save()   # بيعمل السكيمة ويطبّق مهاجرات الفرع تلقائياً
Domain.objects.get_or_create(domain=f"{SUB}.{BASE}", tenant=t, defaults={"is_primary": True})

# 3) مدير داخل الفرع
with schema_context(t.schema_name):
    if not User.objects.filter(username="admin").exists():
        User.objects.create_superuser("admin", "admin@example.com", "غيّر-الباسورد")
print("✅ تم — افتح:", f"https://{SUB}.{BASE}/")
exit()
```

> ملاحظة: موديل `Client` عندك ممكن يطلب حقول زيادة (باقة/صناعة). لو ظهر خطأ ناقص حقل، زوّده في السطر `Client(...)` — أو قوللي وأظبطهولك حسب موديلك.

## الخطوة 8️⃣ — (اختياري) ازرع قالب فك N20
```
docker compose exec web python manage.py tenant_command seed_n20_template --schema=demo
```

## ✅ خلاص!
افتح `https://demo.mousstec.com/` — المفروض يشتغل بشهادة HTTPS تلقائية.

---

## 🔧 أوامر يومية مفيدة
| المهمة | الأمر |
|---|---|
| تحديث الكود بعد push | `git pull && docker compose --env-file .env up -d --build` |
| مهاجرات جديدة | `docker compose exec web python manage.py migrate_schemas --shared && docker compose exec web python manage.py migrate_schemas --tenant` |
| متابعة اللوجز | `docker compose logs -f web` |
| إعادة تشغيل | `docker compose restart` |
| إيقاف الكل | `docker compose down` (البيانات محفوظة في volumes) |

## 💾 باكب قاعدة البيانات (مهم — اعمله دوري)
```
docker compose exec db pg_dumpall -U erp > backup_$(date +%F).sql
```
استرجاع:
```
cat backup_file.sql | docker compose exec -T db psql -U erp
```

> لو كان عندك باكب من السيرفر القديم، استرجعه بالأمر ده بدل عمل فرع جديد يدوي.

## ❓ لو حصلت مشكلة
ابعتلي مخرجات:
```
docker compose ps
docker compose logs --tail=60 web
```
وأنا أحلّها معاك.
