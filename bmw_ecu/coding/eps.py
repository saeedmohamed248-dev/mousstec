"""EPS (Electric Power Steering) — "اسكترا" — coding.

Per the brief: EPS is one of the modules that sleeps on the bus. The
caller MUST run `connection.nm.wake_modules(...)` before this routine,
otherwise SecurityAccess will time out.
"""
from __future__ import annotations

from ..exceptions import CodingError
from ..logging_setup import get_logger
from ..uds.client import UdsClient
from ..uds.security_access import SecurityAccess
from ..uds.services import DiagSession
from .fa_vo import VehicleOrder

log = get_logger(__name__)

DID_EPS_VARIANT = 0xF110


async def code_eps(client: UdsClient, security: SecurityAccess, *,
                   vin: str, vo: VehicleOrder, variant_code: bytes) -> None:
    log.info("EPS coding begin", extra={"vin": vin, "variant": variant_code.hex()})
    try:
        await client.diagnostic_session_control(DiagSession.EXTENDED)
        await security.unlock(vin=vin)
        await client.write_data_by_identifier(DID_EPS_VARIANT, variant_code)
        await client.ecu_reset(0x01)
    except Exception as e:
        raise CodingError(f"EPS coding failed: {e}") from e
    log.info("EPS coding done")
