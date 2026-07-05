#!/usr/bin/env bash
# Mousstec field CLI — one-command setup (Linux / macOS / WSL).
#
#   bash bmw_ecu/scripts/setup_field.sh
#
# Creates a local .venv, installs ONLY the 3 transport deps, writes a
# field.env you fill with your port + CAN IDs, and drops a `./field` wrapper
# so you run `./field ping` instead of the long module path. No database, no
# web server — just enough to talk to the car over a CANable.
#
# Set SKIP_INSTALL=1 to skip the pip step (e.g. offline / already installed).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$REPO_ROOT"

PY="${PYTHON:-python3}"
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "⛔ مفيش Python. نصّب Python 3.10+ الأول." >&2
  exit 1
fi
echo "▶ Python: $("$PY" --version)"

# 1) venv
if [ ! -d .venv ]; then
  echo "▶ بعمل .venv …"
  "$PY" -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

# 2) deps (transport only)
if [ "${SKIP_INSTALL:-0}" != "1" ]; then
  echo "▶ بنصّب python-can can-isotp pyserial …"
  pip install --quiet --upgrade pip
  pip install --quiet python-can can-isotp pyserial
else
  echo "▶ تخطّيت التثبيت (SKIP_INSTALL=1)"
fi

# 3) env file (fill once)
ENV_FILE="$REPO_ROOT/field.env"
if [ ! -f "$ENV_FILE" ]; then
  echo "▶ بعمل field.env (املأه بقيم عربيتك) …"
  cat > "$ENV_FILE" <<'ENVEOF'
# املأ القيم دي حسب الـ CANable وعربيتك، وبعدين: source field.env
# الجهاز: ls /dev/ttyACM* /dev/ttyUSB*  (Linux)  |  /dev/cu.usbmodem*  (Mac)
export BMW_ECU_KDCAN_PORT=/dev/ttyACM0
export BMW_ECU_CAN_TX_ID=0x6F1
export BMW_ECU_CAN_RX_ID=0x612
export BMW_ECU_CAN_BITRATE=500000
ENVEOF
else
  echo "▶ field.env موجود بالفعل — مغيّرتوش."
fi

# 4) ./field wrapper
cat > "$REPO_ROOT/field" <<'WRAPEOF'
#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"
# shellcheck disable=SC1091
source .venv/bin/activate
[ -f field.env ] && source field.env
exec python -m bmw_ecu.scripts.field_cli "$@"
WRAPEOF
chmod +x "$REPO_ROOT/field"

echo ""
echo "✅ تمام. الخطوات:"
echo "   1) عدّل field.env (الـ port والـ CAN IDs)"
echo "   2) ./field ping        # اتأكد الكابل بيكلّم العربية"
echo "   3) ./field read-fa     # اقرا الـ FA الحقيقي"
echo "   4) ./field diagnose --engine N18 --bench"
