"""ISN (Individual Serial Number) extraction from FEM/BDC/CAS.

The ISN is a 32-byte token bound to the immobilizer. Reading it requires:
    - Extended diagnostic session
    - Security Access at the elevated level (FEM: 0x05, CAS: 0x03)
    - ReadDataByIdentifier with the platform-specific DID

The DID below is a placeholder. Production code MUST map per FA/chassis.
"""
from __future__ import annotations

from ..exceptions import IsnMismatch
from ..logging_setup import get_logger
from ..uds.client import UdsClient
from ..uds.security_access import SecurityAccess
from ..uds.services import BmwDID, DiagSession

log = get_logger(__name__)

ISN_LENGTH = 32


class IsnExtractor:
    def __init__(self, client: UdsClient, security: SecurityAccess) -> None:
        self.client = client
        self.security = security

    async def extract(self, *, vin: str, did: int = BmwDID.ISN) -> bytes:
        log.info("ISN extract begin", extra={"vin": vin, "ecu": hex(self.client.ecu_addr)})
        await self.client.diagnostic_session_control(DiagSession.EXTENDED)
        await self.security.unlock(vin=vin)
        isn = await self.client.read_data_by_identifier(did)

        if len(isn) != ISN_LENGTH:
            raise IsnMismatch(
                f"Expected {ISN_LENGTH}-byte ISN, got {len(isn)}",
                got_len=len(isn),
            )

        # Sanity: all-zero or all-FF is a flag for "ECU virgin / bricked".
        if all(b == 0 for b in isn) or all(b == 0xFF for b in isn):
            raise IsnMismatch("ISN is virgin (all 0x00 or 0xFF) — refuse to use")

        log.info("ISN extracted", extra={"prefix": isn[:4].hex()})
        return isn
