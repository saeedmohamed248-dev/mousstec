#!/bin/bash
# «مخ موس» على الماك: أول مرة بيجهّز كل حاجة (دقايق)، وبعد كده بيشتغل على طول.
#   bash run_mac.sh            ← الروبوت
#   bash run_mac.sh echo       ← اسمع المايك بيسمع إيه
#   bash run_mac.sh devices    ← المايكات والسماعات اللي الماك شايفها
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
# الملف ده يا جوه فولدر brain نفسه، يا جنبه (في الـ zip)
if [ -f "$HERE/__init__.py" ]; then ROOT="$(dirname "$HERE")"; PKG="$(basename "$HERE")"; else ROOT="$HERE"; PKG=brain; fi
STATE="${MOUSS_HOME:-$HOME/.mouss-brain}"
VENV="$STATE/venv"

if ! command -v python3 >/dev/null; then
  echo "Python مش متسطب: نزّله من https://www.python.org/downloads/ (3.12) وشغّل الملف تاني"; exit 1
fi
if [ ! -x "$VENV/bin/python" ]; then
  echo "[SETUP] أول مرة: بجهّز البرامج في $VENV …"
  mkdir -p "$STATE"
  python3 -m venv "$VENV"
  "$VENV/bin/python" -m pip install -q --upgrade pip
fi
REQ="$ROOT/$PKG/requirements.txt"
STAMP="$VENV/.requirements"
if ! cmp -s "$REQ" "$STAMP"; then
  "$VENV/bin/python" -m pip install -q -r "$REQ"
  cp "$REQ" "$STAMP"
fi
cd "$ROOT"
exec "$VENV/bin/python" -m "$PKG" "${@:-run}"
