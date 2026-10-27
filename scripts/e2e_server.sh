#!/usr/bin/env bash
# 🎭 سيرفر اختبارات المتصفح (e2e/) — زي الإنتاج: DEBUG=0 + collectstatic + daphne.
# بيجهّز القاعدة والشركة التجريبية (seed_demo --sub e2e) وبعدين يشغّل السيرفر.
#
#   E2E_PASSWORD=Secret123 scripts/e2e_server.sh      # http://e2e.localhost:8000
#   (وفي تيرمنال تاني: cd e2e && E2E_PASSWORD=Secret123 npx playwright test)
set -euo pipefail
cd "$(dirname "$0")/.."
: "${E2E_PASSWORD:?لازم E2E_PASSWORD (نفس كلمة السر اللي الاختبارات هتدخل بيها)}"
: "${SECRET_KEY:?لازم SECRET_KEY}"
export DEBUG="${DEBUG:-0}" BASE_DOMAIN=localhost ENABLE_AI_PREDICTIONS=0
PORT="${E2E_PORT:-8000}"

python manage.py migrate_schemas --shared -v0
python manage.py migrate_schemas --tenant -v0
python manage.py refresh_tenant_template
python manage.py seed_demo --sub e2e --password "$E2E_PASSWORD"
python manage.py collectstatic --noinput -v0
echo "🌐 http://e2e.localhost:${PORT}/ جاهز"
exec daphne -b 127.0.0.1 -p "$PORT" erp_core.asgi:application
