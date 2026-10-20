# 🔁 دليل النشر اليومي — Mouss Tec

الخطوات اللي بتتعمل بعد دمج أي PR على `main`.
لإعداد سيرفر جديد من الصفر (Droplet + DNS + أول فرع ومدير) شوف [`DEPLOY_DIGITALOCEAN.md`](DEPLOY_DIGITALOCEAN.md).

> ⚠️ **أهم نقطة:** الكود متنسخ جوه صورة الدوكر نفسها (`COPY . .` في الـ Dockerfile).
> يعني `git pull` لوحده مش بيغيّر حاجة، و`docker compose restart` هيرجّع نفس الكود القديم.
> **لازم `--build`.**

---

## 1️⃣ النشر المعتاد

> ✅ **قبل أي نشر:** آخر commit على `main` لازم يكون عليه علامة ✅ «CI أخضر» في GitHub
> (تبويب Actions) — يعني السويت كاملة (~٢٣٠٠ اختبار) واختبارات المتصفح عربي وإنجليزي عدّت.
> لو أحمر: ماتنشرش، افتح الـ job اللي وقع وشوف السبب.

```bash
ssh root@IP-السيرفر
cd ~/mousstec/mousstec

# باكب الأول — دقيقة بتوفّر يوم
bash deploy/backup.sh

git pull origin main
docker compose --env-file .env up -d --build

docker compose logs -f web
```

> المجلد الصح هو اللي جواه `docker-compose.yml`. لو المسار عندك مختلف، لاقيه بـ `find ~ -maxdepth 3 -name docker-compose.yml` واستخدمه في كل الأوامر.

استنى السطر ده في اللوج — هو علامة إن كل حاجة قبله (المهاجرات، الستاتيك، الترجمة) عدّت:

```
🌐 تشغيل خادم ASGI (daphne) على المنفذ 8000...
```

اطلع بـ `Ctrl+C` — ده بيوقف متابعة اللوج بس، الخدمة بتفضل شغّالة.

| الحالة | الأمر |
|---|---|
| غيّرت `.env` بس من غير كود | `docker compose --env-file .env up -d` (الـ `restart` مش بيعيد قراءة `.env`) |
| تنضيف الصور القديمة بعد كام نشر | `docker image prune -f` |

---

## 2️⃣ المهاجرات

**في النشر المعتاد مفيش خطوة يدوية.** ملف `deploy/entrypoint.web.sh` بيشتغل مع كل إقلاع لحاوية `web` وبيعمل بالترتيب:

انتظار Postgres ← `migrate_schemas --shared` ← `migrate_schemas --tenant` ← `refresh_tenant_template` ← فحص القوالب ← `compilemessages` ← `collectstatic` ← daphne.

> ⚡ `refresh_tenant_template` بيحدّث schema اسمها `tenant_template` — الشركة الجديدة بتتنسخ منها
> في ثواني بدل ما تشغّل كل المهاجرات (~دقيقة). لو فشل، التسجيل بيرجع للمهاجرات العادية (أبطأ بس شغّال).
> **ماتمسحهاش** — ولو اتمسحت أول تسجيل بيعملها تاني.

شغّلها بإيدك بس لو حاوية `web` وقعت وسط المهاجرات وعايز تشوف الخطأ كامل، أو لو ضفت فرع جديد واتأخّرت مهاجراته:

```bash
docker compose exec web python manage.py migrate_schemas --shared
docker compose exec web python manage.py migrate_schemas --tenant
```

> 🛑 **متستخدمش `manage.py migrate` أبداً** — المشروع multi-tenant و`manage.py` نفسه بيرفض الأمر ده.
> الفرع الجديد لما بيتعمل بـ `Client(...).save()` بيعمل السكيمة ويطبّق مهاجراته تلقائياً.

### 📒 دفتر المطابع المحاسبي (مرة واحدة بعد نشر دورات المطابع)

شركات الطباعة بقت بتتقيّد قيود يومية بالاستحقاق. الأرصدة القديمة (الخزائن، الخامات، الطلبات والدفعات
اللي قبل النشر) بتتقيّد أول مرة حد يفتح «قائمة الدخل» أو «الدفاتر المحاسبية» — بس الأحسن تتعمل مرة
بعد النشر عشان أول فتحة ماتبقاش بطيئة:

```bash
docker compose exec web python manage.py printing_ledger_sync          # كل شركات الطباعة
docker compose exec web python manage.py printing_ledger_sync --force  # مراجعة وتصليح أي فرق (آمن يتعاد)
```

كل سطر بيطبع اسم الشركة و«trial balance balanced» — لو طلع `NOT BALANCED` ابعت اللوج.

> نفس الأمر بيتعاد بعد أي نشر بيغيّر شكل قيود المطابع (`LEDGER_VERSION` في `printing/ledger.py`) —
> زي نشر «حساب لكل خزنة + الأصول والإهلاك + الإشعارات الدائنة + الرواتب المستحقة»: من غير `--force`
> بيعيد مزامنة الشركات اللي دفترها على نسخة أقدم بس.

---

## 3️⃣ متغيرات البيئة

ملف `.env` بيقعد جنب `docker-compose.yml` على السيرفر ومش موجود في git. القالب: `.env.production.example`.

### إجبارية

| المتغير | ملاحظة |
|---|---|
| `SECRET_KEY` | مفيش قيمة افتراضية في `settings.py` — Django بيقع من غيره. ولّده بـ `python3 -c "import secrets;print(secrets.token_urlsafe(64))"` |
| `DEBUG` | `False` على السيرفر دايماً |
| `BASE_DOMAIN` | الدومين من غير `https`. منه بتتبني `ALLOWED_HOSTS` و`CSRF_TRUSTED_ORIGINS` وسَبدومينات الفروع |
| `ACME_EMAIL` | Caddy بيستخدمه في إصدار شهادات Let's Encrypt |
| `EXTRA_ALLOWED_HOSTS` | لازم يفضل `web` — Caddy بينادي `http://web:8000/internal/tls-check/` لشهادات الفروع |
| `POSTGRES_DB` / `POSTGRES_USER` / `POSTGRES_PASSWORD` | حاوية Postgres بتتعمل بيهم أول مرة بس |
| `DATABASE_URL` | `postgres://USER:PASS@db:5432/DB` — لازم يطابق الـ `POSTGRES_*` حرف بحرف، والمضيف `db` مش `localhost` |
| `REDIS_URL` / `CELERY_BROKER_URL` / `CELERY_RESULT_BACKEND` | المضيف `redis` مش `localhost`. لو فاضيين، `docker-compose.yml` بيحط القيم الصح تلقائياً |

### اختيارية

| المتغير | ملاحظة |
|---|---|
| `USE_S3` | `False` = الصور على قرص السيرفر في volume اسمه `media_data`. لو `True` املا مفاتيح `AWS_*` |
| `ENABLE_AI_PREDICTIONS` + `GEMINI_API_KEY` / `AI_VISION_API_KEY` | لو `False` مميزات الذكاء الاصطناعي بتتقفل والنظام بيشتغل عادي |
| `BREVO_API_KEY` | الإيميلات. لازم مفتاح API اللي بيبدأ بـ `xkeysib-` — مفتاح الـ SMTP (`xsmtpsib-`) مش بينفع. اتأكد بـ `bash deploy/check_brevo.sh` |
| `PAYMOB_*` | بوابة الدفع. زرار «ادفع بالكارت» للاشتراكات بيظهر بس لما `PAYMOB_CARD_SUBSCRIPTIONS=True` **و** `PAYMOB_HMAC_SECRET` متظبط — شوف `PAYMOB_SETUP.md` |
| `SIGNUP_EMAIL_VERIFICATION` | `auto` (الافتراضي): لو الإيميل متظبط (Brevo/SMTP) رابط تفعيل الشركة الجديدة بيروح على الإيميل بس؛ غير كده بيظهر على الشاشة. `required` / `off` |
| `TENANT_TEMPLATE_SCHEMA` | اسم قالب تأسيس الشركات (الافتراضي `tenant_template`). فاضي = المهاجرات العادية |
| `FIXIT_SYNC_URL` / `FIXIT_SYNC_SECRET` | مزامنة المخزون والمرتجعات مع موقع FixIt |
| `OMNICHANNEL_SECRET_KEK` | مفتاح Fernet لتشفير توكنات واتساب/ماسنجر بتاعة الفروع |
| `SENTRY_DSN` | تتبّع الأخطاء |

> ⚠️ لو غيّرت `POSTGRES_PASSWORD` بعد أول تشغيل: الحاوية مش بتغيّر الباسورد من نفسها — القيمة دي بتتقرأ مرة واحدة وقت إنشاء قاعدة البيانات. غيّرها جوه Postgres نفسه وعدّل `DATABASE_URL` بنفس القيمة.

---

## 4️⃣ التأكد إن كل حاجة قامت سليمة

### أ. حالة الخدمات

```bash
docker compose ps
```

٦ خدمات: `db` و`redis` بحالة **healthy** (عندهم healthcheck)، و`web` و`worker` و`beat` و`caddy` بحالة **Up**.
أي خدمة بتقول **Restarting** يعني بتقع وبتتعاد — شوف لوجها على طول: `docker compose logs --tail=60 <الاسم>`.

### ب. لوج الويب — الأسطر اللي تدوّر عليها

```bash
docker compose logs --tail=80 web
```

| السطر | معناه |
|---|---|
| `✅ قاعدة البيانات جاهزة` | الاتصال بـ Postgres تمام |
| `🔄 تطبيق مهاجرات ...` | المهاجرات اشتغلت. لو بعدها traceback، دي مشكلتك |
| `⚠️⚠️ فيه قالب/قوالب مكسورة` | النشر بيكمّل، بس الصفحات دي هتطلّع 500 للمستخدمين — راجع الأسطر فوقه |
| `⚠️ فشل compilemessages` | غير مُعطّل — الواجهة الإنجليزية ممكن تبان مخلوطة بالعربي |
| `🌐 تشغيل خادم ASGI (daphne)` | الويب قام. ده السطر الأخير المتوقّع |

### ج. Celery والموقع من بره

```bash
docker compose logs --tail=30 worker   # لازم يقول ready وسامع كل الطوابير
docker compose logs --tail=20 beat
curl -I https://الدومين-بتاعك/         # 200 أو 302
```

لو الـ worker مش سامع `heavy_ai_tasks`، الطيار الآلي وردود واتساب/ماسنجر هتقف في Redis من غير ما تشتغل.
لو `curl` رجّع خطأ شهادة أو مردّش: `docker compose logs --tail=50 caddy`.

### د. الفحص العميق

افتح `/system/health/` من المتصفح وانت داخل بحساب staff (الصفحة محمية بتسجيل الدخول، فـ`curl` عادي مش هيشوفها). بترجّع JSON:

- `status: operational` — كل حاجة تمام
- `status: degraded` — فيه agents واقعة أو circuit مفتوح؛ النظام شغّال بس فيه جزء متعطّل
- `status: critical` — قاعدة البيانات أو Redis مش رادّين

وكمان `db_latency_ms` — فوق ٥٠٠ ملي ثانية بتتحسب بطء وبتفعّل التشغيل المخفّف.

---

## 5️⃣ لو حاجة وقعت

### رجوع الكود لآخر نسخة شغّالة

```bash
git log --oneline -10
git checkout <الـ-commit-القديم>
docker compose --env-file .env up -d --build
```

لو الكود الجديد كان فيه مهاجرات، الرجوع للكود لوحده **مش** بيرجّع قاعدة البيانات لحالتها القديمة. لو المهاجرة هي المشكلة، استرجع الباكب.

### استرجاع قاعدة البيانات

```bash
ls -lh /root/mousstec-backups/
bash deploy/restore.sh /root/mousstec-backups/mousstec_db_XXXX.sql.gz
```

السكريبت بيدّيك ٥ ثواني تلغي بـ `Ctrl+C`، وبعد الاسترجاع بيعيد تشغيل `web` و`worker` و`beat` لوحده.

> 🛑 **متعملش `docker compose down -v`** — `down` لوحده آمن (البيانات في volumes)، لكن `-v` بيمسح الـ volumes: قاعدة البيانات والميديا والشهادات كلها تروح.

### باكب دوري

بدّل المسار بمسار مجلد المشروع عندك:

```
0 3 * * * /root/mousstec/mousstec/deploy/backup.sh >> /var/log/mousstec-backup.log 2>&1
```

`deploy/backup.sh` بيكتب في `/root/mousstec-backups` وبيمسح النسخ الأقدم من ١٤ يوم لوحده.

---

## 6️⃣ الريبو Private — إزاي السيرفر يعمل `git pull`

بعد ما الريبو يبقى Private، `git pull` على السيرفر هيطلب صلاحية. الأأمن **Deploy Key**
(مفتاح للقراءة بس، لريبو واحد بس):

```bash
# على السيرفر
ssh-keygen -t ed25519 -C "mousstec-server" -f ~/.ssh/mousstec_deploy -N ""
cat ~/.ssh/mousstec_deploy.pub
```

في GitHub: الريبو ← **Settings ← Deploy keys ← Add deploy key** ← الصق المفتاح، **من غير**
✔️ Allow write access.

```bash
# على السيرفر: خلّي git يستخدم المفتاح ده لـ GitHub
cat >> ~/.ssh/config <<'CFG'
Host github.com
  IdentityFile ~/.ssh/mousstec_deploy
  IdentitiesOnly yes
CFG
cd ~/mousstec/mousstec
git remote set-url origin git@github.com:saeedmohamed248-dev/mousstec.git
git pull origin main     # أول مرة هيسأل عن بصمة github.com — اكتب yes
```

> 💸 الريبو الـ Private بياخد دقايق GitHub Actions من الباقة (المجانية ٢٠٠٠ دقيقة/شهر). تشغيل CI
> كامل ≈ ٥٠–٦٠ دقيقة-جهاز (٤ أجهزة اختبارات بالتوازي + المتصفح). لو الدقايق خلصت: GitHub Pro/Team،
> أو runner على السيرفر نفسه (Settings ← Actions ← Runners).
