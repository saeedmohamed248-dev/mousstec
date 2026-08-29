#!/usr/bin/env bash
# 🚀 إعداد وتشغيل Mouss Tec على سيرفر جديد بأمر واحد.
#    الاستخدام (من داخل مجلد المشروع):  bash deploy/setup.sh
set -e
cd "$(dirname "$0")/.."   # جذر المشروع

echo "════════════════════════════════════════════"
echo "🚀 إعداد Mouss Tec على السيرفر"
echo "════════════════════════════════════════════"

# 1) swap للسيرفرات صغيرة الرام (أقل من ~1.5 جيجا) لتفادي توقف البناء
MEM=$(free -m 2>/dev/null | awk '/^Mem:/{print $2}' || echo 2000)
if [ "${MEM:-2000}" -lt 1500 ] && [ ! -f /swapfile ]; then
  echo "➕ الرام صغيرة (${MEM}MB) — بإضافة ملف swap بحجم 2 جيجا..."
  fallocate -l 2G /swapfile 2>/dev/null || dd if=/dev/zero of=/swapfile bs=1M count=2048
  chmod 600 /swapfile
  mkswap /swapfile >/dev/null
  swapon /swapfile
  grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  echo "✅ تم تفعيل الـ swap."
fi

# 2) Docker
if ! command -v docker >/dev/null 2>&1; then
  echo "🐳 تثبيت Docker..."
  curl -fsSL https://get.docker.com | sh
fi

# 3) ملف البيئة .env (يتعمل مرة واحدة بقيم عشوائية آمنة)
if [ ! -f .env ]; then
  echo
  echo "📝 إعداد ملف البيئة — محتاج منك حاجتين بس:"
  read -rp "   • الدومين (مثال: mousstec.com): " DOMAIN
  read -rp "   • إيميلك (لشهادة HTTPS المجانية): " EMAIL
  DOMAIN=${DOMAIN:-mousstec.com}
  EMAIL=${EMAIL:-admin@${DOMAIN}}

  SECRET=$(openssl rand -base64 50 | tr -d '\n')
  DBPASS=$(openssl rand -hex 16)

  cat > .env <<EOF
DEBUG=False
SECRET_KEY=${SECRET}
BASE_DOMAIN=${DOMAIN}
ACME_EMAIL=${EMAIL}
EXTRA_ALLOWED_HOSTS=web
POSTGRES_DB=erp_db
POSTGRES_USER=erp
POSTGRES_PASSWORD=${DBPASS}
DATABASE_URL=postgres://erp:${DBPASS}@db:5432/erp_db
REDIS_URL=redis://redis:6379/1
CELERY_BROKER_URL=redis://redis:6379/0
CELERY_RESULT_BACKEND=redis://redis:6379/0
USE_S3=False
# مطفية افتراضياً حتى تضيف مفتاح Gemini (AI_VISION_API_KEY) وتخليها True
ENABLE_AI_PREDICTIONS=False
AI_VISION_API_KEY=
EOF
  echo "✅ اتعمل ملف .env (المفاتيح والباسورد اتولّدوا عشوائيًا وآمنين)."
else
  echo "ℹ️ ملف .env موجود بالفعل — هسيبه زي ما هو."
fi

# 4) التشغيل
echo
echo "🏗️  بناء وتشغيل كل الخدمات (ممكن ياخد ٥–١٠ دقائق أول مرة)..."
docker compose --env-file .env up -d --build

echo
echo "════════════════════════════════════════════"
echo "✅ خلص! الخدمات بتشتغل دلوقتي."
echo "   تابع اللوجز:   docker compose logs -f web"
echo "   حالة الخدمات:  docker compose ps"
echo "════════════════════════════════════════════"
