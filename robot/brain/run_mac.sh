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

# 🐛 [FIX]: python3 اللي جاي مع الماك (3.9) كان بيتاخد على طول — والمخ محتاج 3.10+ (vosk/numpy/
#    faster-whisper مالهمش نسخ لـ 3.9) فالتسطيب كان بيقع بـ «No matching distribution for vosk».
#    بندوّر على أحدث Python متسطب، ولو الـ venv معمول بنسخة قديمة بنعمله من جديد.
PY=""
for c in python3.12 python3.13 python3.11 python3.10 python3; do
  if command -v "$c" >/dev/null && "$c" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
    PY="$c"; break
  fi
done
if [ -z "$PY" ]; then
  echo "محتاج Python 3.12: نزّله من https://www.python.org/downloads/macos/ (اللي مع الماك قديم) وشغّل الملف تاني"; exit 1
fi
if [ -x "$VENV/bin/python" ] && ! "$VENV/bin/python" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
  echo "[SETUP] البيئة القديمة معمولة بـ Python قديم — بعملها من جديد بـ $PY"
  rm -rf "$VENV"
fi
if [ ! -x "$VENV/bin/python" ]; then
  echo "[SETUP] أول مرة: بجهّز البرامج في $VENV …"
  mkdir -p "$STATE"
  "$PY" -m venv "$VENV"
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
