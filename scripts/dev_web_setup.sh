#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# Mouss Tec — تجهيز بيئة تشغيل الموقع داخل Claude Code on the web
#
# شغّله مرة واحدة في الجلسة:   bash scripts/dev_web_setup.sh
# وبعدها:                      python manage.py runserver 0.0.0.0:8000
#
# بيعمل:
#   1) يشغّل PostgreSQL 16 + Redis (الموجودين في الكونتينر)
#   2) ينشئ قاعدة erp_db + يضبط الباسوورد
#   3) يكتب ملف .env للتطوير (DEBUG=True, BASE_DOMAIN=localhost, ...) — لو مش موجود
#   4) يثبّت متطلبات بايثون (يحتاج وصول PyPI)
#   5) يطبّق مهاجرات django-tenants ويزرع شركة تجريبية على demo.localhost
#
# idempotent — تشغيله أكتر من مرة آمن.
# متطلب واحد فقط: وصول الشبكة لـ pypi.org + files.pythonhosted.org
# (يُضبط من Network policy للبيئة على claude.ai/code).
# ─────────────────────────────────────────────────────────────────────────────
set -uo pipefail

PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
cd "$PROJECT_DIR"

PG_BIN="$(ls -d /usr/lib/postgresql/*/bin 2>/dev/null | sort -V | tail -1)"
export PATH="${PG_BIN:-/usr/lib/postgresql/16/bin}:$PATH"

DEMO_PASSWORD='Demo12345'

echo "════════════════════════════════════════════════════════════"
echo " Mouss Tec — dev web setup"
echo "════════════════════════════════════════════════════════════"

# ── 1) Redis ─────────────────────────────────────────────────────────────────
if ! redis-cli ping >/dev/null 2>&1; then
  echo "→ starting redis..."
  redis-server --daemonize yes >/dev/null 2>&1 || true
  sleep 1
fi
redis-cli ping >/dev/null 2>&1 && echo "✅ redis: up" || echo "‼️  redis: NOT running"

# ── 2) PostgreSQL ────────────────────────────────────────────────────────────
if ! pg_isready -q 2>/dev/null; then
  echo "→ starting postgresql cluster..."
  pg_ctlcluster 16 main start 2>/dev/null || true
  sleep 2
fi
if pg_isready -q 2>/dev/null; then
  echo "✅ postgres: up"
  su - postgres -c "psql -tAc \"ALTER USER postgres WITH PASSWORD '123';\"" >/dev/null 2>&1 || true
  if ! su - postgres -c "psql -tAc \"SELECT 1 FROM pg_database WHERE datname='erp_db'\"" 2>/dev/null | grep -q 1; then
    su - postgres -c "createdb erp_db" >/dev/null 2>&1 || true
    echo "✅ database erp_db created"
  else
    echo "ℹ️  database erp_db already exists"
  fi
else
  echo "‼️  postgres: NOT running — DB steps skipped."
fi

# ── 3) ملف .env للتطوير (لا نلمس ملف موجود) ─────────────────────────────────
if [ ! -f .env ]; then
  echo "→ writing dev .env ..."
  cat > .env <<'ENVEOF'
# ⚠️ ملف تطوير محلي فقط — مُولّد من scripts/dev_web_setup.sh (مُتجاهَل في git)
DEBUG=True
SECRET_KEY=dev-only-insecure-key-change-in-prod-0123456789abcdef
BASE_DOMAIN=localhost
DATABASE_URL=postgres://postgres:123@127.0.0.1:5432/erp_db
REDIS_URL=redis://127.0.0.1:6379/1
CELERY_BROKER_URL=redis://127.0.0.1:6379/1
CELERY_RESULT_BACKEND=redis://127.0.0.1:6379/2
SECURE_SSL_REDIRECT=False
ENVEOF
  echo "✅ .env written"
else
  echo "ℹ️  .env already exists — leaving it untouched"
fi

# ── 4) متطلبات بايثون (يحتاج شبكة/PyPI) ──────────────────────────────────────
echo "→ installing python requirements (needs PyPI access)..."
if pip install --no-input --disable-pip-version-check -q -r requirements.txt 2>/tmp/pip_err.log; then
  echo "✅ pip: requirements installed"
else
  echo "‼️  pip install FAILED — PyPI غير متاح على الأرجح (سياسة الشبكة)."
  echo "    الحل: من claude.ai/code افتح إعدادات البيئة → Network access →"
  echo "          اسمح بـ pypi.org و files.pythonhosted.org، ثم ابدأ جلسة جديدة وأعد التشغيل."
  sed 's/^/    [pip] /' /tmp/pip_err.log 2>/dev/null | tail -6
  exit 1
fi

python -c "import django" >/dev/null 2>&1 || { echo "‼️  Django غير متاح بعد التثبيت."; exit 1; }

# ── 5) المهاجرات + زرع شركة تجريبية ──────────────────────────────────────────
if pg_isready -q 2>/dev/null; then
  echo "→ applying shared (public) migrations..."
  python manage.py migrate_schemas --shared --noinput 2>&1 | tail -3

  echo "→ bootstrapping demo tenant (demo.localhost)..."
  python manage.py bootstrap_tenant --base-domain localhost --sub demo \
    --password "$DEMO_PASSWORD" 2>&1 | tail -5
fi

echo ""
echo "════════════════════════════════════════════════════════════"
echo " ✅ جاهز! شغّل الموقع بـ:"
echo "     python manage.py runserver 0.0.0.0:8000"
echo ""
echo "   • الصفحة الرئيسية: http://localhost:8000/"
echo "   • شركة تجريبية:    http://demo.localhost:8000/   (admin / $DEMO_PASSWORD)"
echo "   • الباقات:         http://localhost:8000/pricing/"
echo "════════════════════════════════════════════════════════════"
