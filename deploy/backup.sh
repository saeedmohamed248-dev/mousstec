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

# 🐛 [FIX]: النسخة لو فشلت كانت بتكتب في اللوج بس — ومحدش بيقرا اللوج. دلوقتي أي خروج
#    بغير صفر بيبعت إيميل لصاحب المنصة (manage.py ops_alert). BACKUP_ALERT=0 يقفله.
# 🐛 [FIX]: الإيميل كان بيقول «كود 1» بس — والسبب الحقيقي في اللوج على السيرفر. دلوقتي الإيميل فيه
#    السبب نفسه (FAIL_REASON + آخر سطور الخطأ) والمساحة الفاضية وحالة حاوية الداتابيز.
FAIL_REASON=""
ERR_LOG="$(mktemp)"
_alert_on_failure() {
    local code=$?
    rm -f "${TMP:-}" 2>/dev/null || true
    if [ "$code" != 0 ] && [ "${BACKUP_ALERT:-1}" != 0 ]; then
        local details
        details="السبب: ${FAIL_REASON:-غير معروف (شوف اللوج)}
آخر سطور الخطأ:
$(tail -n 8 "$ERR_LOG" 2>/dev/null || true)
المساحة: $(df -h "$BACKUP_DIR" 2>/dev/null | awk 'NR==2 {print $4 " فاضي من " $2 " (" $5 " مستخدم)"}')
حاوية الداتابيز: $(docker compose ps db --format '{{.Status}}' 2>/dev/null || echo '؟')
اللوج الكامل: /var/log/mousstec-backup.log — كود 1 = نسخة الداتابيز، كود 2 = الملفات المرفوعة أو Spaces."
        docker compose exec -T web python manage.py ops_alert \
            "⛔ النسخة الاحتياطية فشلت (كود $code)" "$details" >/dev/null 2>&1 || true
    fi
    rm -f "$ERR_LOG"
}
trap _alert_on_failure EXIT

STAMP="$(date +%F_%H%M)"
FILE="$BACKUP_DIR/mousstec_db_${STAMP}.sql.gz"

echo "[$(date)] 💾 بدء النسخ الاحتياطي → $FILE"
# 🐛 [FIX]: هارد مليان كان بيبوّظ النسخة في النص من غير سبب واضح — نتأكد الأول (٢ جيجا على الأقل).
FREE_KB="$(df -Pk "$BACKUP_DIR" | awk 'NR==2 {print $4}')"
if [ "${FREE_KB:-0}" -lt "${BACKUP_MIN_FREE_KB:-2097152}" ]; then
    FAIL_REASON="المساحة على الهارد خلصت تقريباً ($((FREE_KB / 1024)) ميجا فاضي) — امسح نسخ قديمة من $BACKUP_DIR أو كبّر الهارد"
    echo "[$(date)] ❌ فشل: $FAIL_REASON" >&2
    exit 1
fi
if ! docker compose exec -T db pg_isready -q 2>>"$ERR_LOG"; then
    FAIL_REASON="حاوية الداتابيز مش شغالة أو مش بترد (docker compose ps db)"
    echo "[$(date)] ❌ فشل: $FAIL_REASON" >&2
    exit 1
fi
# 🐛 [FIX]: اسم المستخدم كان بيتقري بـ grep من .env — لو POSTGRES_USER متكرر
#    في الملف بيطلع "user<سطر جديد>user" والنسخة تفشل. دلوقتي بنستخدم نفس
#    المستخدم اللي حاوية Postgres اتعملت بيه (متغيّر البيئة جواها).
# 🐛 [FIX]: الـ dump الفاشل كان بيسيب ملف ‎.gz‎ حجمه 20 بايت (gzip لمخرجات
#    فاضية) فالفحص «الملف فاضي؟» مايمسكوش — نسخ فاسدة بتتسجّل كأنها اتعملت.
TMP="$FILE.partial"
if ! docker compose exec -T db sh -c 'pg_dumpall -U "$POSTGRES_USER"' 2>>"$ERR_LOG" | gzip > "$TMP"; then
    FAIL_REASON="pg_dumpall رجّع خطأ (التفاصيل تحت)"
    echo "[$(date)] ❌ فشل: pg_dumpall رجّع خطأ — مفيش نسخة اتعملت." >&2
    cat "$ERR_LOG" >&2 || true
    rm -f "$TMP"
    exit 1
fi
# نسخة سليمة لازم تبقى gzip صالح وفيها أوامر SQL فعلاً.
HEAD="$(gunzip -c "$TMP" 2>/dev/null | head -c 4096 || true)"   # || true: SIGPIPE مع pipefail
if ! gzip -t "$TMP" 2>/dev/null || [[ "$HEAD" != *"PostgreSQL database"* ]]; then
    FAIL_REASON="ملف النسخة طلع فاضي أو تالف"
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
    FAIL_REASON="نسخة الملفات المرفوعة فشلت (نسخة الداتابيز سليمة)"
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
                FAIL_REASON="الرفع على Spaces فشل (rclone)"
                echo "[$(date)] ❌ فشل إرسال $(basename "$f") لـ $BACKUP_RCLONE_REMOTE" >&2
            fi
        done
        # 🐛 [FIX]: النسخ على Spaces كانت بتتراكم للأبد (والتخزين بيتحاسب بالجيجا) —
        #    دلوقتي بنمسح اللي أقدم من KEEP_REMOTE_DAYS يوم (افتراضي ٣٠).
        if [ "$OFFSITE_OK" = 1 ]; then
            rclone delete --min-age "${KEEP_REMOTE_DAYS:-30}d" "$BACKUP_RCLONE_REMOTE/" \
                && echo "[$(date)] 🧹 Spaces: اتمسح الأقدم من ${KEEP_REMOTE_DAYS:-30} يوم" \
                || echo "[$(date)] ⚠️ تنظيف Spaces فشل (النسخة نفسها اترفعت)." >&2
        fi
    else
        OFFSITE_OK=0
        FAIL_REASON="rclone مش متسطّب على السيرفر"
        echo "[$(date)] ❌ BACKUP_RCLONE_REMOTE متظبط بس rclone مش متسطّب (curl https://rclone.org/install.sh | bash)." >&2
    fi
fi

# تدوير: حذف النسخ الأقدم من KEEP_DAYS يوم (والملفات من KEEP_MEDIA_DAYS)
find "$BACKUP_DIR" -name 'mousstec_db_*.sql.gz' -mtime +"$KEEP_DAYS" -delete 2>/dev/null || true
find "$BACKUP_DIR" -name 'mousstec_media_*.tar.gz' -mtime +"$KEEP_MEDIA_DAYS" -delete 2>/dev/null || true
echo "[$(date)] 🧹 تم تنظيف النسخ الأقدم من ${KEEP_DAYS} يوم (الملفات: ${KEEP_MEDIA_DAYS})"

# كود خروج غير صفر لو أي جزء فشل — عشان مراقبة الكرون تلاحظ
[ "$MEDIA_OK" = 1 ] && [ "$OFFSITE_OK" = 1 ] || exit 2
