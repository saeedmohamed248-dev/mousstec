"""EWS (Electronic Immobilizer) synchronization.

After ISN injection, the DME and the immobilizer master (FEM/CAS) need
their challenge counters paired. UDS-side this is RoutineControl (0x31)
with the BMW-specific "EWS align" routine ID.
"""
from __future__ import annotations

import asyncio

from ..exceptions import EwsSyncFailed, UdsNegativeResponse
from ..logging_setup import get_logger
from ..uds.client import UdsClient
from ..uds.services import DiagSession

log = get_logger(__name__)

# Placeholder routine ID — adjust per chassis. F-series typically uses 0xAF11.
EWS_ALIGN_ROUTINE_ID = 0xAF11
ROUTINE_START = 0x01
ROUTINE_RESULTS = 0x03


class EwsSync:
    def __init__(self, dme: UdsClient, immobilizer: UdsClient,
                 *, routine_id: int = EWS_ALIGN_ROUTINE_ID) -> None:
        self.dme = dme
        self.immo = immobilizer
        # The EWS-align RoutineControl ID is platform-specific. 0xAF11 is the
        # F-series default; E-series CAS3 differs, so the caller MUST pass the
        # confirmed routine_id rather than rely on the placeholder. We never
        # guess a routine ID against a real immobilizer.
        self.routine_id = routine_id

    async def synchronize(self) -> None:
        log.info("EWS sync begin", extra={"routine": hex(self.routine_id)})
        for client in (self.dme, self.immo):
            await client.diagnostic_session_control(DiagSession.EXTENDED)

        try:
            await self.dme.routine_control(ROUTINE_START, self.routine_id)
            await self.immo.routine_control(ROUTINE_START, self.routine_id)
        except UdsNegativeResponse as e:
            raise EwsSyncFailed(f"EWS routine rejected: NRC=0x{e.nrc:02X}") from e

        # Poll results — both ECUs must report success (last byte == 0x00).
        for _ in range(10):
            await asyncio.sleep(0.5)
            try:
                dme_r = await self.dme.routine_control(ROUTINE_RESULTS, self.routine_id)
                immo_r = await self.immo.routine_control(ROUTINE_RESULTS, self.routine_id)
            except UdsNegativeResponse:
                continue
            if dme_r[-1:] == b"\x00" and immo_r[-1:] == b"\x00":
                log.info("EWS sync complete")
                return
        raise EwsSyncFailed("EWS sync timed out waiting for both ECUs to report OK")
