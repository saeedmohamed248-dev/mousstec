#!/usr/bin/env bash
# ♻️ استرجاع نسخة احتياطية لقاعدة البيانات
#    الاستخدام:  bash deploy/restore.sh /root/mousstec-backups/mousstec_db_XXXX.sql.gz
set -euo pipefail
cd "$(dirname "$0")/.."

FILE="${1:-}"
[ -z "$FILE" ] && { echo "الاستخدام: bash deploy/restore.sh <ملف.sql.gz>"; exit 1; }
[ ! -f "$FILE" ] && { echo "❌ الملف مش موجود: $FILE"; exit 1; }

PG_USER="$(grep -E '^POSTGRES_USER=' .env 2>/dev/null | cut -d= -f2- | tr -d '\r')"
PG_USER="${PG_USER:-erp}"

echo "⚠️  هيتم الكتابة فوق قاعدة البيانات الحالية بمحتوى: $FILE"
echo "    اضغط Ctrl+C خلال 5 ثواني للإلغاء..."
sleep 5

echo "♻️  جاري الاسترجاع..."
gunzip -c "$FILE" | docker compose exec -T db psql -U "$PG_USER" -d postgres

echo "✅ تم الاسترجاع. جاري إعادة تشغيل التطبيق..."
docker compose restart web worker beat
echo "✅ خلص."
