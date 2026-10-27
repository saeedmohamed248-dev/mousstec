#!/bin/bash
# «مخ موس» على الـ mini PC (Ubuntu 24.04): بيركّب كل حاجة، وبيخلّي الروبوت
# يشتغل لوحده أول ما الجهاز يفتح — من غير شاشة ولا كيبورد.
#   bash install_ubuntu.sh
# بعدها:  journalctl --user -u mouss-brain -f   (اللوج)
#         systemctl --user restart mouss-brain  (بعد ما تعدّل brain.ini)
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
if [ -f "$HERE/__init__.py" ]; then ROOT="$(dirname "$HERE")"; PKG="$(basename "$HERE")"; else ROOT="$HERE"; PKG=brain; fi
STATE="${MOUSS_HOME:-$HOME/.mouss-brain}"
VENV="$STATE/venv"

echo "[SETUP] برامج النظام (هيطلب الباسورد)…"
sudo apt-get update -q
sudo apt-get install -y -q python3-venv python3-pip libportaudio2

echo "[SETUP] بايثون والموديل في $VENV …"
mkdir -p "$STATE"
[ -x "$VENV/bin/python" ] || python3 -m venv "$VENV"
"$VENV/bin/python" -m pip install -q --upgrade pip
"$VENV/bin/python" -m pip install -q -r "$ROOT/$PKG/requirements.txt"

if [ ! -f "$ROOT/brain.ini" ] && [ ! -f "$STATE/brain.ini" ]; then
  cp "$ROOT/$PKG/brain.ini.example" "$STATE/brain.ini"
  echo "[SETUP] ⚠️ اكتب الـ api_base والـ token في $STATE/brain.ini وبعدين: systemctl --user restart mouss-brain"
fi

mkdir -p "$HOME/.config/systemd/user"
cat > "$HOME/.config/systemd/user/mouss-brain.service" <<EOF
[Unit]
Description=Mouss Tec robot brain
After=network-online.target sound.target pipewire.service

[Service]
WorkingDirectory=$ROOT
ExecStart=$VENV/bin/python -m $PKG run
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload
systemctl --user enable --now mouss-brain
# من غير دي، الخدمة مش بتقوم غير لما حد يعمل login
sudo loginctl enable-linger "$USER"
# 🐛 [FIX]: من غير login محدش بياخد صلاحية المايك والسماعة (بتتدّى للي قاعد قدام
# الجهاز بس)، فالروبوت كان بيبقى أطرش وأخرس لحد ما حد يدخل.
sudo usermod -aG audio "$USER"
# 🐛 [FIX]: Ubuntu بينام لوحده (حتى على شاشة الدخول بعد 20 دقيقة) فالروبوت بيقف
# في نص اليوم. الجهاز ده للروبوت بس: مفيش نوم.
sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target >/dev/null
echo "[SETUP] ✅ خلاص. اللوج: journalctl --user -u mouss-brain -f"
echo "[SETUP] اعمل restart للجهاز مرة واحدة علشان صلاحية الصوت تشتغل: sudo reboot"
