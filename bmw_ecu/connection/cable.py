"""CANable / D-CAN cable configuration helper.

The `ConnectionManager` already knows how to open a CANable (K+DCAN over a
python-can ``slcan`` adapter); this module just makes building its
`TransportConfig` a one-liner for the API layer and the tech, from either
environment variables or a request body — with the same hard rule the rest
of the stack follows: **the per-ECU CAN arbitration IDs are never guessed**;
the caller must supply them (from the workshop / EcuHardwareProfile).

Sensible R56/E-series defaults are applied only to the *non-identifying*
knobs (500 kbit D-CAN bitrate, ``slcan`` bustype, 11-bit addressing).

The blue FTDI "K+DCAN" cable is NOT a python-can adapter and cannot be
driven — use a CANable/slcan adapter. `describe_adapter_hint()` returns
that guidance for the UI so the tech isn't left guessing why an FTDI cable
won't open.
"""
from __future__ import annotations

import os
from typing import Any, Optional

from .base import TransportConfig, TransportKind

# D-CAN on E-/R-series runs at 500 kbit; K-CAN body bus at 100 kbit. The
# diagnostic path (DME/CAS) is on D-CAN, so 500k is the right default.
_DEFAULT_BITRATE = 500_000
_DEFAULT_BUSTYPE = "slcan"


class CableConfigError(ValueError):
    """Raised when a cable config is missing a required, un-guessable field."""


def _parse_int(value: Any, field: str) -> int:
    """Accept 0x612 / '0x612' / '1554' / 1554 → int. Raises on garbage."""
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return int(value.strip(), 0)   # 0 base → honours 0x / 0o / decimal
        except ValueError as e:
            raise CableConfigError(f"{field} is not a valid integer: {value!r}") from e
    raise CableConfigError(f"{field} is required")


def cable_config(*,
                 serial_port: str,
                 can_tx_id: Any,
                 can_rx_id: Any,
                 bitrate: int = _DEFAULT_BITRATE,
                 can_interface: str = _DEFAULT_BUSTYPE,
                 extended_id: bool = False,
                 timeout: float = 5.0) -> TransportConfig:
    """Build a CANable D-CAN `TransportConfig`. Required: serial_port + the
    tester/ECU CAN ID pair (never guessed)."""
    if not serial_port:
        raise CableConfigError(
            "serial_port is required (e.g. '/dev/ttyACM0' or "
            "'/dev/cu.usbmodem1411' for a CANable/slcan adapter)")
    return TransportConfig(
        kind=TransportKind.KDCAN,
        serial_port=serial_port,
        can_interface=can_interface or _DEFAULT_BUSTYPE,
        bitrate=int(bitrate) if bitrate else _DEFAULT_BITRATE,
        can_tx_id=_parse_int(can_tx_id, "can_tx_id"),
        can_rx_id=_parse_int(can_rx_id, "can_rx_id"),
        can_extended_id=bool(extended_id),
        timeout=float(timeout) if timeout else 5.0,
    )


def cable_config_from_env() -> Optional[TransportConfig]:
    """Build the cable config from BMW_ECU_* env vars, or None if the port
    isn't set. Mirrors ConnectionManager's env contract exactly."""
    port = os.environ.get("BMW_ECU_KDCAN_PORT")
    tx = os.environ.get("BMW_ECU_CAN_TX_ID")
    rx = os.environ.get("BMW_ECU_CAN_RX_ID")
    if not (port and tx and rx):
        return None
    return cable_config(
        serial_port=port,
        can_tx_id=tx,
        can_rx_id=rx,
        bitrate=int(os.environ.get("BMW_ECU_CAN_BITRATE", "500000"), 0),
        can_interface=os.environ.get("BMW_ECU_CAN_INTERFACE", _DEFAULT_BUSTYPE),
    )


def cable_config_from_request(body: dict[str, Any]) -> TransportConfig:
    """Build the cable config from a request body. Falls back to env for any
    field the caller omits, so the UI can send just the CAN IDs when the port
    is fixed in the server env."""
    env_port = os.environ.get("BMW_ECU_KDCAN_PORT")
    env_tx = os.environ.get("BMW_ECU_CAN_TX_ID")
    env_rx = os.environ.get("BMW_ECU_CAN_RX_ID")
    return cable_config(
        serial_port=(body.get("serial_port") or env_port or ""),
        can_tx_id=(body.get("can_tx_id") if body.get("can_tx_id") is not None
                   else env_tx),
        can_rx_id=(body.get("can_rx_id") if body.get("can_rx_id") is not None
                   else env_rx),
        bitrate=body.get("bitrate") or _DEFAULT_BITRATE,
        can_interface=body.get("can_interface") or _DEFAULT_BUSTYPE,
        extended_id=bool(body.get("extended_id", False)),
    )


def describe_adapter_hint() -> dict[str, str]:
    """UI guidance so a tech isn't stuck on why an FTDI cable won't open."""
    return {
        "ar": "استخدم أدابتر CANable/slcan (بيظهر كـ /dev/ttyACM* أو usbmodem). "
              "الكابل الأزرق FTDI K+DCAN مش أدابتر python-can ومش هيفتح.",
        "en": "Use a CANable/slcan adapter (shows up as /dev/ttyACM* or "
              "usbmodem). The blue FTDI K+DCAN cable is not a python-can "
              "adapter and will not open.",
    }
