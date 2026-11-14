#!/usr/bin/env bash
# 🔄 خطوة النشر اللي بتتعمل مرة واحدة قبل تشغيل نسخ الويب:
#    مهاجرات ← قالب الشركات ← فحص القوالب ← الملفات الثابتة.
#    (كانت جوه entrypoint.web.sh — ومع أكتر من نسخة ويب كانوا هيشغّلوها كلهم في نفس الوقت.)
#    docker compose بيشغّلها لوحده قبل الويب (depends_on: service_completed_successfully)،
#    و deploy/deploy.sh بيشغّلها بـ «docker compose run --rm migrate».
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

# ⚠️ المشروع multi-tenant: migrate_schemas (migrate العادي مقفول عمداً)
echo "🔄 مهاجرات الجداول المشتركة (public)..."
python manage.py migrate_schemas --shared

echo "🔄 مهاجرات جداول الشركات (${MIGRATE_EXECUTOR:-multiprocessing})..."
python manage.py migrate_schemas --tenant --executor="${MIGRATE_EXECUTOR:-multiprocessing}" \
  || echo "⚠️⚠️ مهاجرات بعض الشركات فشلت (أو مفيش شركات لسه) — راجع الأسطر فوق قبل ما تكمّل."

echo "⚡ تحديث قالب الشركات الجديدة..."
python manage.py refresh_tenant_template || echo "⚠️ فشل تحديث قالب الشركات — التسجيل هيستخدم المهاجرات العادية."

echo "🧪 فحص صياغة القوالب..."
python manage.py check_templates --all || echo "⚠️⚠️ فيه قالب/قوالب مكسورة! راجع الأسطر فوق."

# الملفات الثابتة في volume مشترك بين نسخ الويب — بتتجمع هنا مرة واحدة
# (من غير --clear: النسخ القديمة اللي لسه شغالة وقت النشر بتفضل لاقية ملفاتها)
echo "🎨 تجميع الملفات الثابتة..."
python manage.py collectstatic --noinput

# 🖼️ صور منتجات كتالوج السوق اللي جوه المشروع (clients/catalog_photos) — المنتجات اللي ليها صورة بتتعدّى
echo "🖼️ صور منتجات الكتالوج..."
python manage.py import_catalog_images --bundled || echo "⚠️ رفع صور الكتالوج فشل — الموقع شغال عادي، والمنتجات هتفضل بالأيقونة."

echo "✅ خطوة النشر خلصت."
