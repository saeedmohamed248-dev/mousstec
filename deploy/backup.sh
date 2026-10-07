#!/usr/bin/env bash
# 💾 نسخة احتياطية يومية لقاعدة بيانات Mouss Tec (مع تدوير تلقائي)
#    الاستخدام:  bash deploy/backup.sh
#    كرون يومي:  0 3 * * * /root/mousstec/deploy/backup.sh >> /var/log/mousstec-backup.log 2>&1
set -euo pipefail
cd "$(dirname "$0")/.."   # جذر المشروع (فيه docker-compose.yml + .env)

BACKUP_DIR="${BACKUP_DIR:-/root/mousstec-backups}"
KEEP_DAYS="${KEEP_DAYS:-14}"
mkdir -p "$BACKUP_DIR"

STAMP="$(date +%F_%H%M)"
FILE="$BACKUP_DIR/mousstec_db_${STAMP}.sql.gz"

echo "[$(date)] 💾 بدء النسخ الاحتياطي → $FILE"
# 🐛 [FIX]: اسم المستخدم كان بيتقري بـ grep من .env — لو POSTGRES_USER متكرر
#    في الملف بيطلع "user<سطر جديد>user" والنسخة تفشل. دلوقتي بنستخدم نفس
#    المستخدم اللي حاوية Postgres اتعملت بيه (متغيّر البيئة جواها).
# 🐛 [FIX]: الـ dump الفاشل كان بيسيب ملف ‎.gz‎ حجمه 20 بايت (gzip لمخرجات
#    فاضية) فالفحص «الملف فاضي؟» مايمسكوش — نسخ فاسدة بتتسجّل كأنها اتعملت.
TMP="$FILE.partial"
if ! docker compose exec -T db sh -c 'pg_dumpall -U "$POSTGRES_USER"' | gzip > "$TMP"; then
    echo "[$(date)] ❌ فشل: pg_dumpall رجّع خطأ — مفيش نسخة اتعملت." >&2
    rm -f "$TMP"
    exit 1
fi
# نسخة سليمة لازم تبقى gzip صالح وفيها أوامر SQL فعلاً.
HEAD="$(gunzip -c "$TMP" 2>/dev/null | head -c 4096 || true)"   # || true: SIGPIPE مع pipefail
if ! gzip -t "$TMP" 2>/dev/null || [[ "$HEAD" != *"PostgreSQL database"* ]]; then
    echo "[$(date)] ❌ فشل: ملف النسخة فاضي أو تالف — تم حذفه." >&2
    rm -f "$TMP"
    exit 1
fi
mv "$TMP" "$FILE"

SIZE="$(du -h "$FILE" | cut -f1)"
echo "[$(date)] ✅ تمت النسخة بنجاح (${SIZE})"

# تدوير: حذف النسخ الأقدم من KEEP_DAYS يوم
find "$BACKUP_DIR" -name 'mousstec_db_*.sql.gz' -mtime +"$KEEP_DAYS" -delete 2>/dev/null || true
echo "[$(date)] 🧹 تم تنظيف النسخ الأقدم من ${KEEP_DAYS} يوم"
