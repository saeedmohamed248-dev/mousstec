#!/usr/bin/env bash
# 💾 نسخة احتياطية يومية لقاعدة بيانات Mouss Tec + الملفات المرفوعة (مع تدوير، ونسخة برّه السيرفر اختيارياً)
#    الاستخدام:  bash deploy/backup.sh
#    كرون يومي:  0 3 * * * /root/mousstec/deploy/backup.sh >> /var/log/mousstec-backup.log 2>&1
set -euo pipefail
cd "$(dirname "$0")/.."   # جذر المشروع (فيه docker-compose.yml + .env)

BACKUP_DIR="${BACKUP_DIR:-/root/mousstec-backups}"
KEEP_DAYS="${KEEP_DAYS:-14}"
KEEP_MEDIA_DAYS="${KEEP_MEDIA_DAYS:-7}"        # الملفات حجمها كبير — أسبوع كفاية
# ☁️ نسخة برّه السيرفر (اختياري): rclone متسطّب ومتظبط + مثلاً
#    BACKUP_RCLONE_REMOTE=spaces:mousstec-backups/prod   (DigitalOcean Spaces / S3 / Google Drive)
BACKUP_RCLONE_REMOTE="${BACKUP_RCLONE_REMOTE:-}"
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

# 🐛 [FIX]: الملفات المرفوعة (صور القطع، كشوف البنك، مستندات، صور الحضور...) ماكانتش
#    داخلة في أي نسخة — لو قرص السيرفر راح، الداتابيز ترجع والملفات لأ.
MEDIA_FILE="$BACKUP_DIR/mousstec_media_${STAMP}.tar.gz"
MEDIA_OK=1
if docker compose exec -T web tar czf - -C /app/media . > "$MEDIA_FILE.partial" \
        && gzip -t "$MEDIA_FILE.partial" 2>/dev/null; then
    mv "$MEDIA_FILE.partial" "$MEDIA_FILE"
    echo "[$(date)] ✅ نسخة الملفات المرفوعة ($(du -h "$MEDIA_FILE" | cut -f1))"
else
    rm -f "$MEDIA_FILE.partial"
    MEDIA_OK=0
    echo "[$(date)] ⚠️ نسخة الملفات المرفوعة فشلت (نسخة الداتابيز سليمة)." >&2
fi

# 🐛 [FIX]: النسخ كانت على نفس السيرفر بس — لو السيرفر وقع أو اتمسح، النسخ تروح معاه.
OFFSITE_OK=1
if [ -n "$BACKUP_RCLONE_REMOTE" ]; then
    if command -v rclone >/dev/null 2>&1; then
        TO_SEND=("$FILE")
        [ "$MEDIA_OK" = 1 ] && TO_SEND+=("$MEDIA_FILE")
        for f in "${TO_SEND[@]}"; do
            if rclone copy "$f" "$BACKUP_RCLONE_REMOTE/" ; then
                echo "[$(date)] ☁️ اتبعتت برّه السيرفر: $(basename "$f")"
            else
                OFFSITE_OK=0
                echo "[$(date)] ❌ فشل إرسال $(basename "$f") لـ $BACKUP_RCLONE_REMOTE" >&2
            fi
        done
    else
        OFFSITE_OK=0
        echo "[$(date)] ❌ BACKUP_RCLONE_REMOTE متظبط بس rclone مش متسطّب (curl https://rclone.org/install.sh | bash)." >&2
    fi
fi

# تدوير: حذف النسخ الأقدم من KEEP_DAYS يوم (والملفات من KEEP_MEDIA_DAYS)
find "$BACKUP_DIR" -name 'mousstec_db_*.sql.gz' -mtime +"$KEEP_DAYS" -delete 2>/dev/null || true
find "$BACKUP_DIR" -name 'mousstec_media_*.tar.gz' -mtime +"$KEEP_MEDIA_DAYS" -delete 2>/dev/null || true
echo "[$(date)] 🧹 تم تنظيف النسخ الأقدم من ${KEEP_DAYS} يوم (الملفات: ${KEEP_MEDIA_DAYS})"

# كود خروج غير صفر لو أي جزء فشل — عشان مراقبة الكرون تلاحظ
[ "$MEDIA_OK" = 1 ] && [ "$OFFSITE_OK" = 1 ] || exit 2
