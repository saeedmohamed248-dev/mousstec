#!/usr/bin/env bash
# Launch the ERP server in REAL, hardware-locked mode for the bench rig.
#
# Simulator can NEVER engage here (BMW_ECU_REQUIRE_HARDWARE=1). If no CANable
# / ENET interface answers, the Coding Room shows an honest "Hardware Not
# Found" instead of fake data. Reliability over convenience.
#
# Usage:
#   1. cp bmw_ecu/scripts/canable.env.example bmw_ecu/scripts/canable.env
#   2. edit canable.env — fill BMW_ECU_KDCAN_PORT + the two CAN IDs
#   3. bash bmw_ecu/scripts/run_real_bench.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

# shellcheck disable=SC1091
source venv/bin/activate

ENV_FILE="bmw_ecu/scripts/canable.env"
if [[ -f "$ENV_FILE" ]]; then
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  echo "✓ loaded $ENV_FILE"
else
  echo "⚠ $ENV_FILE not found — running real mode with no CAN config."
  echo "  Connect will honestly report 'Hardware Not Found' until you create it."
  export BMW_ECU_REQUIRE_HARDWARE=1
  unset BMW_ECU_SIMULATOR || true
  export BMW_ECU_BACKUP_ROOT="${BMW_ECU_BACKUP_ROOT:-/tmp/mousstec_backups}"
fi

echo "── BMW ECU runtime mode ──────────────────────────────"
echo "  REQUIRE_HARDWARE = ${BMW_ECU_REQUIRE_HARDWARE:-<unset>}"
echo "  SIMULATOR        = ${BMW_ECU_SIMULATOR:-<unset>}  (must be unset)"
echo "  KDCAN_PORT       = ${BMW_ECU_KDCAN_PORT:-<unset>}"
echo "  CAN_TX/RX        = ${BMW_ECU_CAN_TX_ID:-<unset>} / ${BMW_ECU_CAN_RX_ID:-<unset>}"
echo "──────────────────────────────────────────────────────"

exec python manage.py runserver 0.0.0.0:8000
