"""Domain exceptions for the BMW ECU subsystem.

Every exception carries a stable `code` so the cloud-sync recorder and the
UI can branch on machine-readable values, not message strings.
"""
from __future__ import annotations

from typing import Any


class BmwEcuError(Exception):
    """Base error. All subsystem exceptions inherit from this."""

    code: str = "BMW_ECU_GENERIC"

    def __init__(self, message: str = "", **context: Any) -> None:
        super().__init__(message or self.code)
        self.context: dict[str, Any] = context


# --- Connection layer -------------------------------------------------------
class ConnectionError_(BmwEcuError):
    code = "CONNECTION_FAILED"


class TransportTimeout(ConnectionError_):
    code = "TRANSPORT_TIMEOUT"


class NoInterfaceDetected(ConnectionError_):
    code = "NO_INTERFACE_DETECTED"


# --- Hardware / wiring (chatbot translates these to actionable instructions) ---
class HardwareWiringError(BmwEcuError):
    code = "HARDWARE_WIRING"


class ECUNoPowerError(HardwareWiringError):
    code = "ECU_NO_POWER"


class CANLinesReversedError(HardwareWiringError):
    code = "CAN_LINES_REVERSED"


class IgnitionOffError(HardwareWiringError):
    code = "IGNITION_OFF"


class BoxNotConnectedError(HardwareWiringError):
    code = "SMART_BOX_NOT_CONNECTED"


# --- Billing ----------------------------------------------------------------
class BillingError(BmwEcuError):
    code = "BILLING_ERROR"


class FeeAuthorizationDeclined(BillingError):
    code = "FEE_AUTH_DECLINED"


# --- Safety layer -----------------------------------------------------------
class SafetyAbort(BmwEcuError):
    """Raised when a pre-flight or post-flight safety gate fails.

    Catching this MUST trigger rollback if a write was in flight.
    """

    code = "SAFETY_ABORT"


class LowVoltage(SafetyAbort):
    code = "LOW_VOLTAGE"


class BackupRequired(SafetyAbort):
    code = "BACKUP_REQUIRED"


class BackupVerificationFailed(SafetyAbort):
    code = "BACKUP_VERIFICATION_FAILED"


# --- UDS layer --------------------------------------------------------------
class UdsNegativeResponse(BmwEcuError):
    code = "UDS_NRC"

    def __init__(self, sid: int, nrc: int, message: str = "") -> None:
        super().__init__(message or f"SID=0x{sid:02X} NRC=0x{nrc:02X}", sid=sid, nrc=nrc)
        self.sid = sid
        self.nrc = nrc


class SecurityAccessDenied(BmwEcuError):
    code = "SECURITY_ACCESS_DENIED"


# --- ISN / Coding -----------------------------------------------------------
class IsnMismatch(BmwEcuError):
    code = "ISN_MISMATCH"


class EwsSyncFailed(BmwEcuError):
    code = "EWS_SYNC_FAILED"


class CodingError(BmwEcuError):
    code = "CODING_ERROR"


# --- Flashing ---------------------------------------------------------------
class FlashError(BmwEcuError):
    code = "FLASH_ERROR"


class ChecksumMismatch(FlashError):
    code = "CHECKSUM_MISMATCH"


class FlashRolledBack(FlashError):
    """Flash failed and recovery completed — ECU is back to the known-good state."""

    code = "FLASH_ROLLED_BACK"


class FlashRollbackFailed(FlashError):
    """Rollback itself failed. Vehicle is in an unknown state — DO NOT POWER CYCLE."""

    code = "FLASH_ROLLBACK_FAILED"
