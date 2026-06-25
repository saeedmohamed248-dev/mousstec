"""DME (engine ECU) coding.

UDS path:
    1. 0x10 0x03 extended session
    2. 0x27 security
    3. 0x2E F015 write coding index
    4. 0x2E F1A1..F1AF for per-feature flags (per FA)
    5. 0x11 reset
"""
from __future__ import annotations

from ..exceptions import CodingError
from ..logging_setup import get_logger
from ..uds.client import UdsClient
from ..uds.security_access import SecurityAccess
from ..uds.services import DiagSession
from .fa_vo import VehicleOrder

log = get_logger(__name__)

DID_CODING_INDEX = 0xF015


async def code_dme(client: UdsClient, security: SecurityAccess, *,
                   vin: str, vo: VehicleOrder, coding_index: bytes) -> None:
    log.info("DME coding begin", extra={"vin": vin, "options": len(vo.options)})
    try:
        await client.diagnostic_session_control(DiagSession.EXTENDED)
        await security.unlock(vin=vin)
        await client.write_data_by_identifier(DID_CODING_INDEX, coding_index)
        # Per-feature flags would loop here based on VO mapping table.
        await client.ecu_reset(0x01)
    except Exception as e:
        raise CodingError(f"DME coding failed: {e}") from e
    log.info("DME coding done")
