"""Thin async UDS client over an AbstractTransport.

Implements the SIDs we actually use: 0x10, 0x22, 0x27, 0x2E, 0x31, 0x34/36/37.
For everything else, fall through to `raw_request`.
"""
from __future__ import annotations

import asyncio
from typing import Optional

from ..connection.base import AbstractTransport
from ..exceptions import UdsNegativeResponse
from ..logging_setup import bind, get_logger
from .services import NEG_RESP, NRC, SID, DiagSession

log = get_logger(__name__)


class UdsClient:
    def __init__(self, transport: AbstractTransport, *, ecu_addr: int,
                 session_name: str = "unnamed") -> None:
        self.transport = transport
        self.ecu_addr = ecu_addr
        self.log = bind(log, ecu=hex(ecu_addr), sess=session_name)

    # --- Core round-trip ---------------------------------------------------
    async def raw_request(self, payload: bytes, *, timeout: float = 5.0) -> bytes:
        self.log.debug(f"TX {payload.hex(' ')}")
        resp = await self.transport.request(self.ecu_addr, payload, timeout=timeout)
        self.log.debug(f"RX {resp.hex(' ')}")

        # Handle 0x78 (response pending) — keep waiting up to 10s.
        deadline = asyncio.get_running_loop().time() + 10.0
        while (resp and resp[0] == NEG_RESP and len(resp) >= 3
               and resp[2] == NRC.RESPONSE_PENDING):
            if asyncio.get_running_loop().time() > deadline:
                break
            resp = await self.transport.recv(timeout=timeout)
            self.log.debug(f"RX(pending) {resp.hex(' ')}")

        if resp and resp[0] == NEG_RESP and len(resp) >= 3:
            sid_req, nrc = resp[1], resp[2]
            raise UdsNegativeResponse(sid_req, nrc)
        return resp

    # --- High-level services ----------------------------------------------
    async def diagnostic_session_control(self, session: DiagSession) -> bytes:
        return await self.raw_request(bytes([SID.DIAGNOSTIC_SESSION_CONTROL, session]))

    async def tester_present(self) -> bytes:
        return await self.raw_request(bytes([SID.TESTER_PRESENT, 0x00]))

    async def read_data_by_identifier(self, did: int) -> bytes:
        resp = await self.raw_request(
            bytes([SID.READ_DATA_BY_IDENT, (did >> 8) & 0xFF, did & 0xFF])
        )
        # Strip echo: [0x62, DID_hi, DID_lo, ...data]
        return resp[3:]

    async def write_data_by_identifier(self, did: int, data: bytes) -> bytes:
        return await self.raw_request(
            bytes([SID.WRITE_DATA_BY_IDENT, (did >> 8) & 0xFF, did & 0xFF]) + data
        )

    async def clear_diagnostic_information(self, group: int = 0xFFFFFF) -> bytes:
        """UDS 0x14 ClearDiagnosticInformation. Default group 0xFFFFFF = all DTCs."""
        return await self.raw_request(bytes([
            SID.CLEAR_DIAGNOSTIC_INFORMATION,
            (group >> 16) & 0xFF, (group >> 8) & 0xFF, group & 0xFF,
        ]))

    async def routine_control(self, sub_func: int, routine_id: int,
                              data: bytes = b"") -> bytes:
        return await self.raw_request(
            bytes([SID.ROUTINE_CONTROL, sub_func,
                   (routine_id >> 8) & 0xFF, routine_id & 0xFF]) + data
        )

    async def ecu_reset(self, reset_type: int = 0x01) -> Optional[bytes]:
        try:
            return await self.raw_request(bytes([SID.ECU_RESET, reset_type]), timeout=2.0)
        except Exception:
            # ECU may bounce before responding — that's normal.
            return None

    # --- Tester-present keepalive -----------------------------------------
    def keepalive_task(self, interval_s: float = 2.0) -> asyncio.Task:
        """Spawn a background task that pings TesterPresent every interval."""
        async def _loop() -> None:
            try:
                while True:
                    await asyncio.sleep(interval_s)
                    try:
                        await self.tester_present()
                    except Exception as e:
                        self.log.warning(f"keepalive: {e}")
            except asyncio.CancelledError:
                pass
        return asyncio.create_task(_loop(), name=f"uds_keepalive_{hex(self.ecu_addr)}")
