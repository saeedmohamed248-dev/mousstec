#!/usr/bin/env bash
# 🚀 نقطة تشغيل خدمة الويب: انتظار قاعدة البيانات → مهاجرات → ستاتيك → daphne
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

# ⚠️ المشروع multi-tenant: نستخدم migrate_schemas (migrate العادي مقفول عمداً)
echo "🔄 تطبيق مهاجرات الجداول المشتركة (public)..."
python manage.py migrate_schemas --shared

echo "🔄 تطبيق مهاجرات جداول الفروع (tenants)..."
python manage.py migrate_schemas --tenant || echo "ℹ️ لا توجد فروع بعد — عادي في أول تشغيل."

echo "🎨 تجميع الملفات الثابتة..."
python manage.py collectstatic --noinput

echo "🌐 تشغيل خادم ASGI (daphne) على المنفذ 8000..."
exec daphne -b 0.0.0.0 -p 8000 erp_core.asgi:application
