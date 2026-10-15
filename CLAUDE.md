# Mouss Tec — تعليمات العمل

## التواصل
- كلّم صاحب المشروع **بالعربي (مصري)** دايماً — الردود والملخّصات ووصف الـ PR.
- الكود والتعليقات بنفس أسلوب الملف اللي بتعدّل فيه (التعليقات عربي، وعلامة `🐛 [FIX]:` قبل شرح أي تصليح).

## أسلوب الشغل
- الهدف: سيستم بمستوى الشركات العالمية. أي مشكلة أو باج تقابلك وانت شغال —
  حتى لو برا المطلوب — صلّحها واختبرها واذكرها في الملخّص، طالما التصليح صغير وواضح.
  التغييرات الكبيرة (موديلات/مهاجرات/تغيير سلوك فلوس) اعرضها الأول.
- أي تصليح في الفلوس (خزائن، آجل، تحصيل، مرتجعات) لازم اختبار يثبته.
- «عميل نقدي» (`Customer.is_walk_in`) مشترك بين ناس كتير: ممنوع يتسجّل عليه آجل،
  والتحصيل عليه من الفاتورة نفسها. الاسم المكتوب من غير رقم بيتحفظ في ملاحظات الفاتورة
  (`SaleInvoice.walk_in_name` / `customer_display_name`).
- الصفحات عربي RTL: أي تاريخ+وقت أو رقم موبايل جوه نص عربي يتلف في `<span dir="ltr">`.
- أي جملة جديدة في القوالب تتلف في `{% trans %}` وتتضاف ترجمتها في
  `locale/en/LC_MESSAGES/django.po` وبعدين `msgfmt` للـ `.mo`.

## الاختبارات
- المشروع multi-tenant (django-tenants) على Postgres — `manage.py migrate` مقفول؛ استخدم `migrate_schemas`.
- CI (`.github/workflows/ci.yml`): compileall + `scripts/lint_templates.py` + ruff (قواعد محددة) +
  `makemigrations --check` + `check_templates` + 3 ملفات اختبار.
- محلياً: `service postgresql start` و`redis-server --daemonize yes` (من غير Redis كل كلاس اختبار
  بيستنى دقيقة)، و Python 3.12 (`numpy==1.26.4` مالوش wheel لـ 3.13).
  ```bash
  SECRET_KEY=x DATABASE_URL=postgres://postgres:123@localhost:5432/erp_db DEBUG=0 ENABLE_AI_PREDICTIONS=0 \
    python manage.py test inventory.tests.<module>
  ```

## النشر
- يدوي على DigitalOcean عن طريق SSH — الخطوات في `docs/DEPLOY_RUNBOOK.md`
  (`git pull` + `docker compose --env-file .env up -d --build`؛ الـ `--build` إجباري).
