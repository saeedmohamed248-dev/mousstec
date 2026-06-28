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
from .isn_map import IsnAccessSpec, isn_spec_for_profile

log = get_logger(__name__)

ISN_LENGTH = 32


class IsnNotOverUds(RuntimeError):
    """This family's ISN must be read on the bench (BDM/EEPROM), not via UDS."""


class IsnSpecUnverified(RuntimeError):
    """The ISN DID/level for this family hasn't been confirmed on hardware."""


class IsnExtractor:
    def __init__(self, client: UdsClient, security: SecurityAccess) -> None:
        self.client = client
        self.security = security

    async def extract(self, *, vin: str, did: int = BmwDID.ISN,
                      security_level: int | None = None,
                      length: int = ISN_LENGTH) -> bytes:
        """Low-level read: extended session → unlock → RDBI(did)."""
        log.info("ISN extract begin", extra={"vin": vin, "ecu": hex(self.client.ecu_addr)})
        await self.client.diagnostic_session_control(DiagSession.EXTENDED)
        await self.security.unlock(vin=vin, level=security_level)
        isn = await self.client.read_data_by_identifier(did)

        if len(isn) != length:
            raise IsnMismatch(
                f"Expected {length}-byte ISN, got {len(isn)}",
                got_len=len(isn),
            )

        # Sanity: all-zero or all-FF is a flag for "ECU virgin / bricked".
        if all(b == 0 for b in isn) or all(b == 0xFF for b in isn):
            raise IsnMismatch("ISN is virgin (all 0x00 or 0xFF) — refuse to use")

        log.info("ISN extracted", extra={"prefix": isn[:4].hex()})
        return isn

    async def extract_for_profile(self, *, vin: str, profile,
                                  spec: IsnAccessSpec | None = None,
                                  allow_unverified: bool = False) -> bytes:
        """Read the ISN using the correct DID + security level for the ECU
        family, instead of a single hard-coded DID.

        Guards that protect a real car:
          • families whose ISN is bench-only (over_uds=False) are refused over
            UDS — caller must use the bench/BDM path;
          • an unverified spec (DID/level not confirmed on hardware) is refused
            unless `allow_unverified=True` is passed deliberately.
        """
        spec = spec or isn_spec_for_profile(profile)
        if not spec.over_uds:
            raise IsnNotOverUds(
                f"{spec.family} ISN is not readable over UDS — use the bench "
                f"(BDM/EEPROM) path. {spec.notes}"
            )
        if not spec.verified and not allow_unverified:
            raise IsnSpecUnverified(
                f"ISN spec for '{spec.family}' is unverified (DID "
                f"0x{spec.did:04X}, level 0x{spec.security_level:02X}). Confirm "
                f"it per FA, or pass allow_unverified=True. {spec.notes}"
            )
        return await self.extract(
            vin=vin, did=spec.did, security_level=spec.security_level,
            length=spec.length,
        )
