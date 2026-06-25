"""FEM/BDC coding (central body domain controller, F/G series)."""
from __future__ import annotations

from ..exceptions import CodingError
from ..logging_setup import get_logger
from ..uds.client import UdsClient
from ..uds.security_access import SecurityAccess
from ..uds.services import DiagSession
from .fa_vo import VehicleOrder

log = get_logger(__name__)

DID_FEM_CODING = 0xF050


async def code_fem(client: UdsClient, security: SecurityAccess, *,
                   vin: str, vo: VehicleOrder, coding_blob: bytes) -> None:
    log.info("FEM coding begin", extra={"vin": vin})
    try:
        await client.diagnostic_session_control(DiagSession.EXTENDED)
        await security.unlock(vin=vin)
        await client.write_data_by_identifier(DID_FEM_CODING, coding_blob)
        await client.ecu_reset(0x01)
    except Exception as e:
        raise CodingError(f"FEM coding failed: {e}") from e
    log.info("FEM coding done")
