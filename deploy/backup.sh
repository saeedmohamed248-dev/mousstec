#!/usr/bin/env bash
# 💾 نسخة احتياطية يومية لقاعدة بيانات Mouss Tec (مع تدوير تلقائي)
#    الاستخدام:  bash deploy/backup.sh
#    كرون يومي:  0 3 * * * /root/mousstec/deploy/backup.sh >> /var/log/mousstec-backup.log 2>&1
set -euo pipefail
cd "$(dirname "$0")/.."   # جذر المشروع (فيه docker-compose.yml + .env)

BACKUP_DIR="${BACKUP_DIR:-/root/mousstec-backups}"
KEEP_DAYS="${KEEP_DAYS:-14}"
mkdir -p "$BACKUP_DIR"

PG_USER="$(grep -E '^POSTGRES_USER=' .env 2>/dev/null | cut -d= -f2- | tr -d '\r')"
PG_USER="${PG_USER:-erp}"

STAMP="$(date +%F_%H%M)"
FILE="$BACKUP_DIR/mousstec_db_${STAMP}.sql.gz"

echo "[$(date)] 💾 بدء النسخ الاحتياطي → $FILE"
docker compose exec -T db pg_dumpall -U "$PG_USER" | gzip > "$FILE"

# تحقّق إن الملف مش فاضي (فشل الـ dump)
if [ ! -s "$FILE" ]; then
    echo "[$(date)] ❌ فشل: ملف النسخة فاضي — تم حذفه." >&2
    rm -f "$FILE"
    exit 1
fi

SIZE="$(du -h "$FILE" | cut -f1)"
echo "[$(date)] ✅ تمت النسخة بنجاح (${SIZE})"

# تدوير: حذف النسخ الأقدم من KEEP_DAYS يوم
find "$BACKUP_DIR" -name 'mousstec_db_*.sql.gz' -mtime +"$KEEP_DAYS" -delete 2>/dev/null || true
echo "[$(date)] 🧹 تم تنظيف النسخ الأقدم من ${KEEP_DAYS} يوم"
