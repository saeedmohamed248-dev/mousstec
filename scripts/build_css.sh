#!/usr/bin/env bash
# 🎨 بناء CSS بتاع Tailwind من القوالب (نفس نسخة 3.4.17 اللي كانت شغالة في المتصفح).
#
#   scripts/build_css.sh           # يبني static/vendor/css/tailwind.css
#   scripts/build_css.sh --check   # CI: يفشل لو الملف المحفوظ مش متحدّث مع القوالب
#
# أداة Tailwind (standalone — من غير node) بتتنزل مرة في .cache/.
set -euo pipefail
cd "$(dirname "$0")/.."
VERSION=3.4.17
OUT=static/vendor/css/tailwind.css

case "$(uname -s)-$(uname -m)" in
  Linux-x86_64)  PLATFORM=linux-x64 ;;
  Linux-aarch64) PLATFORM=linux-arm64 ;;
  Darwin-arm64)  PLATFORM=macos-arm64 ;;
  Darwin-x86_64) PLATFORM=macos-x64 ;;
  *) echo "منصة مش مدعومة: $(uname -s)-$(uname -m)" >&2; exit 1 ;;
esac
BIN=".cache/tailwindcss-${VERSION}-${PLATFORM}"
if [ ! -x "$BIN" ]; then
  mkdir -p .cache
  curl -sSfL -o "$BIN.tmp" \
    "https://github.com/tailwindlabs/tailwindcss/releases/download/v${VERSION}/tailwindcss-${PLATFORM}"
  chmod +x "$BIN.tmp" && mv "$BIN.tmp" "$BIN"
fi

build() { "$BIN" -c tailwind.config.js -i scripts/tailwind/input.css -o "$1" --minify 2>/dev/null; }

if [ "${1:-}" = "--check" ]; then
  tmp="$(mktemp)"
  build "$tmp"
  if ! cmp -s "$tmp" "$OUT"; then
    echo "::error file=${OUT}::CSS بتاع Tailwind مش متحدّث مع القوالب — شغّل: make css" >&2
    exit 1
  fi
  echo "✅ ${OUT} متحدّث"
else
  build "$OUT"
  echo "✅ ${OUT} ($(wc -c < "$OUT") bytes)"
fi
