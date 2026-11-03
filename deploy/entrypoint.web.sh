#!/usr/bin/env bash
# 🚀 نقطة تشغيل خدمة الويب: انتظار قاعدة البيانات → ترجمة → daphne (المهاجرات في entrypoint.migrate.sh)
set -e

echo "⏳ في انتظار قاعدة البيانات (Postgres)..."
until python - <<'PY' 2>/dev/null
import os, urllib.parse as u, psycopg2
d = u.urlparse(os.environ["DATABASE_URL"])
psycopg2.connect(
    dbname=d.path[1:], user=d.username, password=d.password,
    host=d.hostname, port=d.port or 5432, connect_timeout=3,
).close()
PY
do
  sleep 2
done
echo "✅ قاعدة البيانات جاهزة."

# 🔄 المهاجرات/قالب الشركات/فحص القوالب/الستاتيك بقت في deploy/entrypoint.migrate.sh — بتتعمل
#    مرة واحدة قبل نسخ الويب (ممكن يبقى فيه أكتر من نسخة شغالين مع بعض).
#    RUN_MIGRATIONS=1 يرجّعها هنا (تشغيل يدوي لكونتينر لوحده من غير compose).
if [ "${RUN_MIGRATIONS:-0}" = "1" ]; then
  bash deploy/entrypoint.migrate.sh
fi

# 🌍 تجميع ملفات الترجمة (.po → .mo) عشان الموقع الإنجليزي يظهر كامل بعد كل
#    نشر — لو الـ .mo قديم أو ناقص بيظهر خليط عربي/إنجليزي. gettext متثبّت في
#    الصورة. غير مُعطِّل: لو فشل بيكمّل بالـ .mo المرفوعة في الريبو.
echo "🌍 تجميع ملفات الترجمة (compilemessages)..."
python manage.py compilemessages -l ar -l en || echo "⚠️ فشل compilemessages — هيستخدم ملفات .mo المرفوعة."

echo "🌐 تشغيل خادم ASGI (daphne) على المنفذ 8000..."
exec daphne -b 0.0.0.0 -p 8000 erp_core.asgi:application
