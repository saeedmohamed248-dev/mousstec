"""D-CAN through the FTDI K+DCAN cable — BMW-FAST framing, echo, read-only scan.

No cable needed: a fake serial port plays a MINI R56 D-CAN bus behind the cable.
It models the details that break naive code on real cables:
  • some cable firmwares echo the request back, some don't,
  • a late/foreign frame from another ECU can sit in front of our reply,
  • an ECU may answer 0x7F xx 0x78 (response pending) before the real reply.
"""
from __future__ import annotations

import asyncio
import io
import unittest
from typing import Callable, Optional

from bmw_ecu.connection.base import TransportConfig, TransportKind
from bmw_ecu.connection.dcan_cable import DCanCableTransport
from bmw_ecu.connection.kline import decode_kwp, encode_kwp
from bmw_ecu.connection.manager import ConnectionManager
from bmw_ecu.exceptions import TransportTimeout, UdsNegativeResponse
from bmw_ecu.management.commands.bmw_dcan_scan import Command
from bmw_ecu.scan import kwp_eseries
from bmw_ecu.uds.client import UdsClient

TESTER = 0xF1
VIN = "WMWMF31090TX12345"

Responder = Callable[[int, bytes], Optional[list[bytes]]]


class _FakeCable:
    """pyserial stand-in: the cable + the car's ECUs behind it.

    ``responder(ecu_addr, request_data)`` returns the list of reply DATA
    payloads that ECU sends (None ⇒ the ECU isn't on the bus).
    """

    def __init__(self, responder: Responder, *, echo: bool = False,
                 noise: bytes = b"") -> None:
        self.responder = responder
        self.echo = echo
        self.noise = noise
        self._rx = bytearray()
        self.timeout = 1.0
        self.writes: list[bytes] = []

    def write(self, frame: bytes) -> int:
        self.writes.append(bytes(frame))
        if self.echo:
            self._rx.extend(frame)
        self._rx.extend(self.noise)
        ecu = frame[1]
        replies = self.responder(ecu, decode_kwp(frame))
        for data in replies or []:
            self._rx.extend(encode_kwp(TESTER, ecu, data))
        return len(frame)

    def flush(self) -> None:
        pass

    def read(self, n: int = 1) -> bytes:
        chunk = bytes(self._rx[:n])
        del self._rx[:n]
        return chunk

    def reset_input_buffer(self) -> None:
        self._rx.clear()

    def close(self) -> None:
        pass


class _FakeCableTransport(DCanCableTransport):
    """The real transport with its serial port swapped for the fake cable."""

    def __init__(self, fake: _FakeCable, **cfg) -> None:
        super().__init__(TransportConfig(
            kind=TransportKind.DCAN_CABLE, serial_port="/dev/fake-kdcan",
            timeout=0.2, **cfg))
        self._fake = fake

    async def open(self) -> None:
        self._ser = self._fake
        self._connected = True


IDENT_FRM = bytes([0x5A, 0x80]) + b"\x00\x00" + b"6135919081" + b"\x01FRM3_R56"


def r56_car(dtcs: Optional[dict[int, list[tuple[int, int]]]] = None) -> Responder:
    """FRM (0x72) + CAS (0x40) answer; CAS knows the VIN, FRM doesn't."""
    dtcs = dtcs or {}

    def responder(ecu: int, req: bytes) -> Optional[list[bytes]]:
        if ecu not in (0x72, 0x40):
            return None
        if req == bytes([0x1A, 0x80]):
            return [IDENT_FRM if ecu == 0x72 else bytes([0x5A, 0x80]) + b"CAS3_9147"]
        if req == bytes([0x1A, 0x90]):
            if ecu == 0x40:
                return [bytes([0x5A, 0x90]) + VIN.encode()]
            return [bytes([0x7F, 0x1A, 0x12])]  # FRM: sub-function not supported
        if req == bytes([0x18, 0x02, 0xFF, 0xFF]):
            entries = dtcs.get(ecu, [])
            body = b"".join(bytes([c >> 8, c & 0xFF, s]) for c, s in entries)
            return [bytes([0x58, len(entries)]) + body]
        if req == bytes([0x14, 0xFF, 0xFF]):
            return [bytes([0x54, 0xFF, 0xFF])]
        return [bytes([0x7F, req[0], 0x11])]
    return responder


def _run(coro):
    return asyncio.run(coro)


class DCanCableTransportTests(unittest.TestCase):
    def test_requires_serial_port(self) -> None:
        with self.assertRaises(ValueError):
            DCanCableTransport(TransportConfig(kind=TransportKind.DCAN_CABLE))

    def test_request_is_bmw_fast_frame_to_the_client_ecu(self) -> None:
        fake = _FakeCable(r56_car())

        async def go() -> bytes:
            t = _FakeCableTransport(fake)
            await t.open()
            return await t.request(0x72, bytes([0x1A, 0x80]))

        self.assertEqual(_run(go()), IDENT_FRM)
        # [0x80|len] [ECU] [tester] [data] [checksum] — no fast-init, no 0x81.
        self.assertEqual(fake.writes, [encode_kwp(0x72, TESTER, bytes([0x1A, 0x80]))])

    def test_echo_of_own_request_is_dropped(self) -> None:
        fake = _FakeCable(r56_car(), echo=True)

        async def go() -> bytes:
            t = _FakeCableTransport(fake)
            await t.open()
            return await t.request(0x72, bytes([0x1A, 0x80]))

        self.assertEqual(_run(go()), IDENT_FRM)

    def test_foreign_frame_before_reply_is_skipped(self) -> None:
        # A late reply from the DME (0x12) sits in front of the FRM's answer.
        late = encode_kwp(TESTER, 0x12, bytes([0x5A, 0x80, 0x01]))
        fake = _FakeCable(r56_car(), echo=True, noise=late)

        async def go() -> bytes:
            t = _FakeCableTransport(fake)
            await t.open()
            return await t.request(0x72, bytes([0x1A, 0x80]))

        self.assertEqual(_run(go()), IDENT_FRM)

    def test_silent_ecu_times_out(self) -> None:
        fake = _FakeCable(r56_car(), echo=True)

        async def go() -> None:
            t = _FakeCableTransport(fake)
            await t.open()
            await t.request(0x12, bytes([0x1A, 0x80]))

        with self.assertRaises(TransportTimeout):
            _run(go())

    def test_response_pending_then_answer(self) -> None:
        def responder(ecu: int, req: bytes):
            return [bytes([0x7F, 0x18, 0x78]), bytes([0x58, 0x00])]

        async def go() -> list:
            t = _FakeCableTransport(_FakeCable(responder))
            await t.open()
            return await kwp_eseries.read_dtcs(UdsClient(t, ecu_addr=0x72))

        self.assertEqual(_run(go()), [])

    def test_manager_builds_cable_transport_from_env(self) -> None:
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {"BMW_ECU_DCAN_CABLE_PORT": "COM3"}):
            cands = ConnectionManager._detection_candidates()
        cable = [c for c in cands if c.kind is TransportKind.DCAN_CABLE]
        self.assertEqual(len(cable), 1)
        self.assertEqual(cable[0].serial_port, "COM3")
        self.assertEqual(cable[0].dcan_cable_baudrate, 115_200)
        self.assertIsInstance(ConnectionManager._build(cable[0]), DCanCableTransport)


class KwpEseriesTests(unittest.TestCase):
    def _client(self, responder: Responder, addr: int) -> tuple[UdsClient, _FakeCableTransport]:
        t = _FakeCableTransport(_FakeCable(responder))
        return UdsClient(t, ecu_addr=addr), t

    def test_parse_dtcs(self) -> None:
        resp = bytes([0x58, 2, 0x9C, 0xB2, 0x24, 0xA0, 0xB4, 0x20])
        self.assertEqual(
            [d.to_dict() for d in kwp_eseries.parse_dtcs(resp)],
            [{"code": "9CB2", "status": "0x24"}, {"code": "A0B4", "status": "0x20"}])

    def test_parse_dtcs_keeps_complete_entries_of_truncated_reply(self) -> None:
        resp = bytes([0x58, 2, 0x9C, 0xB2, 0x24, 0xA0])
        self.assertEqual(len(kwp_eseries.parse_dtcs(resp)), 1)

    def test_parse_dtcs_rejects_wrong_service(self) -> None:
        with self.assertRaises(ValueError):
            kwp_eseries.parse_dtcs(bytes([0x5A, 0x80]))

    def test_ident_vin_and_dtcs_from_frm_and_cas(self) -> None:
        car = r56_car(dtcs={0x72: [(0x9CB2, 0x24)]})

        async def go():
            frm, t = self._client(car, 0x72)
            await t.open()
            cas = UdsClient(t, ecu_addr=0x40)
            return (await kwp_eseries.read_ident(frm),
                    await kwp_eseries.read_vin(frm),
                    await kwp_eseries.read_vin(cas),
                    await kwp_eseries.read_dtcs(frm))

        ident, frm_vin, cas_vin, dtcs = _run(go())
        self.assertIn("6135919081", ident.strings)
        self.assertIsNone(frm_vin)        # FRM refuses 1A 90 → None, no crash
        self.assertEqual(cas_vin, VIN)
        self.assertEqual([d.code_hex for d in dtcs], ["9CB2"])

    def test_clear_dtcs(self) -> None:
        async def go():
            c, t = self._client(r56_car(), 0x72)
            await t.open()
            await kwp_eseries.clear_dtcs(c)
            return t._fake.writes

        self.assertEqual(_run(go())[-1], encode_kwp(0x72, TESTER, bytes([0x14, 0xFF, 0xFF])))

    def test_negative_ident_raises(self) -> None:
        async def go():
            c, t = self._client(lambda ecu, req: [bytes([0x7F, 0x1A, 0x11])], 0x72)
            await t.open()
            await kwp_eseries.read_ident(c)

        with self.assertRaises(UdsNegativeResponse):
            _run(go())


class DcanScanCommandTests(unittest.TestCase):
    def _scan(self, fake: _FakeCable, targets: list[int]) -> tuple[dict, str]:
        out = io.StringIO()
        cmd = Command(stdout=out)
        report = _run(cmd.scan(_FakeCableTransport(fake), targets,
                               timeout=0.2, read_dtc=True))
        return report, out.getvalue()

    def test_scan_finds_frm_and_cas_with_vin_and_faults(self) -> None:
        fake = _FakeCable(r56_car(dtcs={0x72: [(0x9CB2, 0x24)]}), echo=True)
        targets = [m.address for m in kwp_eseries.r56_candidates()]
        report, out = self._scan(fake, targets)

        found = {m["address"]: m for m in report["modules"]}
        self.assertEqual(set(found), {"0x72", "0x40"})
        self.assertEqual(found["0x72"]["dtcs"], [{"code": "9CB2", "status": "0x24"}])
        self.assertEqual(found["0x40"]["dtcs"], [])
        self.assertEqual(report["vins"], [VIN])
        self.assertIn("وحدة الإضاءة (FRM)", out)

    def test_scan_is_read_only(self) -> None:
        fake = _FakeCable(r56_car(), echo=True)
        self._scan(fake, [0x72, 0x40, 0x12])
        sids = {decode_kwp(f)[0] for f in fake.writes}
        # Only ident (0x1A) and read-fault-memory (0x18) — never clear/write/security.
        self.assertEqual(sids, {0x1A, 0x18})

    def test_parse_targets(self) -> None:
        self.assertEqual(Command._parse_targets("0x72, 64"), [0x72, 0x40])


if __name__ == "__main__":
    unittest.main()
