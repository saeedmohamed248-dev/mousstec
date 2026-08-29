#!/usr/bin/env bash
# 🔎 تشخيص مفتاح Brevo بأمان — من غير ما تكشف المفتاح في الشات.
# التشغيل على السيرفر من مجلد المشروع:
#     bash deploy/check_brevo.sh
#
# بيقولك: نوع المفتاح، طوله، وهل Brevo بيقبله ولا لأ — من غير ما يطبع المفتاح.

set -u

ENV_FILE="${1:-.env}"

if [ ! -f "$ENV_FILE" ]; then
  echo "❌ مش لاقي ملف $ENV_FILE — شغّل الأمر من جوّه مجلد المشروع."
  exit 1
fi

# نقرأ القيمة ونشيل أي مسافات/علامات تنصيص
RAW=$(grep -E '^BREVO_API_KEY=' "$ENV_FILE" | head -n1 | cut -d= -f2-)
KEY=$(printf '%s' "$RAW" | tr -d '"'"'"' \r\n' | xargs 2>/dev/null)

echo "════════════════════════════════════════════"
echo "🔎 فحص BREVO_API_KEY في: $ENV_FILE"
echo "────────────────────────────────────────────"

if [ -z "$KEY" ]; then
  echo "❌ المفتاح فاضي أو السطر مش موجود."
  exit 1
fi

LEN=${#KEY}
PREFIX=${KEY:0:9}
LAST4=${KEY: -4}

echo "• الطول: $LEN حرف"
echo "• أول 9 حروف: ${PREFIX}…"
echo "• آخر 4 حروف: …${LAST4}"

case "$KEY" in
  xkeysib-*)
    echo "• النوع: ✅ مفتاح API صحيح (xkeysib-)"
    ;;
  xsmtpsib-*)
    echo "• النوع: ❌ ده مفتاح SMTP (xsmtpsib-) — مش هينفع مع الـ API."
    echo "  اعمل مفتاح جديد من: SMTP & API ▸ تبويب (API Keys) ▸ Generate a new API key"
    echo "  المفتاح الصح لازم يبدأ بـ xkeysib-"
    exit 2
    ;;
  *)
    echo "• النوع: ⚠️ مش معروف — المفروض يبدأ بـ xkeysib-"
    ;;
esac

# لو في مسافات جوّه القيمة الأصلية
if printf '%s' "$RAW" | grep -q ' '; then
  echo "⚠️ في مسافة جوّه القيمة في .env — امسحها."
fi

echo "────────────────────────────────────────────"
echo "📡 بنسأل Brevo نفسها هل المفتاح شغّال…"

CODE=$(curl -s -o /tmp/brevo_check.json -w '%{http_code}' \
  -H "api-key: $KEY" \
  -H "accept: application/json" \
  https://api.brevo.com/v3/account)

if [ "$CODE" = "200" ]; then
  echo "✅ Brevo قبل المفتاح (200 OK). المفتاح سليم."
  echo "   لو الإرسال لسه بيفشل، غالباً الـ Sender مش Verified أو DEFAULT_FROM_EMAIL غلط."
elif [ "$CODE" = "401" ]; then
  echo "❌ Brevo رفض المفتاح (401). المفتاح ده مش موجود/متلغي عندهم."
  echo "   اعمل مفتاح جديد: SMTP & API ▸ API Keys ▸ Generate a new API key، وحطّه في .env"
else
  echo "⚠️ رجع كود $CODE:"
  cat /tmp/brevo_check.json 2>/dev/null | head -c 250
  echo
fi
rm -f /tmp/brevo_check.json
echo "════════════════════════════════════════════"
