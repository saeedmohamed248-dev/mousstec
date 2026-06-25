"""In-process UDS ECU simulator.

Lets the entire subsystem be unit-tested with no hardware:
    transport = MockTransport(ecu=MockEcu(...))
    client = UdsClient(transport, ecu_addr=0x40)
    await client.diagnostic_session_control(DiagSession.EXTENDED)

`MockEcu` implements the same Seed-Key handshake that
`MockSeedKeyProvider` expects — so SecurityAccess works end-to-end.
"""
from __future__ import annotations

import asyncio
import os
from typing import Optional

from ..connection.base import AbstractTransport, TransportConfig, TransportKind
from ..uds.seed_key_providers import MockSeedKeyProvider
from ..uds.services import NEG_RESP, NRC, SID


class MockEcu:
    """A scriptable, deterministic UDS responder."""

    def __init__(self, *, vin: str = "WBA0000MOCK00000", isn: Optional[bytes] = None,
                 battery_volts: float = 13.4) -> None:
        self.vin = vin
        self.isn = isn or bytes(range(32))  # deterministic 00..1F
        self.battery_volts = battery_volts
        self.session = 0x01
        self._security_unlocked = False
        self._pending_seed: Optional[bytes] = None
        self.memory: dict[int, bytes] = {}  # address -> bytes (for upload/download)
        self._sk_provider = MockSeedKeyProvider()

    def handle(self, payload: bytes) -> bytes:
        if not payload:
            return self._nrc(0x00, NRC.GENERAL_REJECT)
        sid = payload[0]

        if sid == SID.DIAGNOSTIC_SESSION_CONTROL:
            self.session = payload[1]
            return bytes([0x50, payload[1], 0x00, 0x32, 0x01, 0xF4])

        if sid == SID.TESTER_PRESENT:
            return bytes([0x7E, payload[1] if len(payload) > 1 else 0x00])

        if sid == SID.SECURITY_ACCESS:
            return self._handle_security(payload)

        if sid == SID.READ_DATA_BY_IDENT:
            return self._handle_rdbi(payload)

        if sid == SID.WRITE_DATA_BY_IDENT:
            return self._handle_wdbi(payload)

        if sid == SID.ROUTINE_CONTROL:
            return bytes([0x71]) + payload[1:4] + b"\x00"

        if sid == SID.ECU_RESET:
            self._security_unlocked = False
            self.session = 0x01
            return bytes([0x51, payload[1]])

        return self._nrc(sid, NRC.SERVICE_NOT_SUPPORTED)

    # --- Handlers ----------------------------------------------------------
    def _handle_security(self, payload: bytes) -> bytes:
        sub = payload[1]
        if sub % 2 == 1:  # request seed
            if self._security_unlocked:
                return bytes([0x67, sub]) + bytes(4)  # all zeros = already unlocked
            seed = bytes((sub * 17 + i) & 0xFF for i in range(4))
            self._pending_seed = seed
            return bytes([0x67, sub]) + seed
        else:  # send key
            if self._pending_seed is None:
                return self._nrc(SID.SECURITY_ACCESS, NRC.REQUEST_SEQUENCE_ERROR)
            expected = self._sk_provider.compute_key(self._pending_seed)
            received = payload[2:]
            self._pending_seed = None
            if received != expected:
                return self._nrc(SID.SECURITY_ACCESS, NRC.INVALID_KEY)
            self._security_unlocked = True
            return bytes([0x67, sub])

    def _handle_rdbi(self, payload: bytes) -> bytes:
        did = (payload[1] << 8) | payload[2]
        if did == 0xF190:  # VIN
            data = self.vin.encode("ascii")
        elif did == 0xF40C:  # battery voltage (raw centivolts, big-endian)
            data = int(self.battery_volts * 100).to_bytes(2, "big")
        elif did == 0xF1A0:  # ISN — only when unlocked
            if not self._security_unlocked:
                return self._nrc(SID.READ_DATA_BY_IDENT, NRC.SECURITY_ACCESS_DENIED)
            data = self.isn
        else:
            return self._nrc(SID.READ_DATA_BY_IDENT, NRC.REQUEST_OUT_OF_RANGE)
        return bytes([0x62, payload[1], payload[2]]) + data

    def _handle_wdbi(self, payload: bytes) -> bytes:
        did = (payload[1] << 8) | payload[2]
        data = payload[3:]
        if did == 0xF1A0:  # ISN
            if not self._security_unlocked:
                return self._nrc(SID.WRITE_DATA_BY_IDENT, NRC.SECURITY_ACCESS_DENIED)
            if len(data) != 32:
                return self._nrc(SID.WRITE_DATA_BY_IDENT, NRC.REQUEST_OUT_OF_RANGE)
            self.isn = data
        else:
            # Accept arbitrary DID writes when unlocked.
            if not self._security_unlocked:
                return self._nrc(SID.WRITE_DATA_BY_IDENT, NRC.SECURITY_ACCESS_DENIED)
            self.memory[did] = data
        return bytes([0x6E, payload[1], payload[2]])

    @staticmethod
    def _nrc(sid: int, nrc: int) -> bytes:
        return bytes([NEG_RESP, sid, nrc])


class MockTransport(AbstractTransport):
    """In-memory transport that routes UDS frames to a `MockEcu`."""

    kind = TransportKind.DOIP  # pretend; doesn't matter

    def __init__(self, ecu: MockEcu, *, config: Optional[TransportConfig] = None) -> None:
        super().__init__(config or TransportConfig(kind=TransportKind.DOIP, host="mock"))
        self.ecu = ecu
        self._inbox: asyncio.Queue[bytes] = asyncio.Queue()

    async def open(self) -> None:
        self._connected = True

    async def close(self) -> None:
        self._connected = False

    async def send(self, target_addr: int, payload: bytes) -> None:
        # Simulate <1ms ECU processing.
        if os.environ.get("BMW_ECU_MOCK_LATENCY"):
            await asyncio.sleep(float(os.environ["BMW_ECU_MOCK_LATENCY"]))
        resp = self.ecu.handle(payload)
        await self._inbox.put(resp)

    async def recv(self, timeout: Optional[float] = None) -> bytes:
        return await asyncio.wait_for(self._inbox.get(), timeout=timeout or 5.0)
