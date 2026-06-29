"""Real multi-frame ISO-TP over the D-CAN (K+DCAN) transport.

Proves the rewritten `KDCANTransport` exchanges payloads LARGER than a single
CAN frame (>7 bytes) — i.e. real First-Frame + Consecutive-Frames + Flow-Control
segmentation/reassembly, which is the hard requirement for WriteDataByIdentifier
and flashing.

We run it over python-can's in-process **virtual** bus (no hardware needed):
two transports with swapped tx/rx IDs talk to each other. If python-can /
can-isotp aren't installed, the whole module is skipped so the main suite stays
green (the libs are an optional, hardware-only dependency).
"""
from __future__ import annotations

import asyncio
import unittest

try:  # optional hardware deps — skip the module if absent
    import can  # type: ignore  # noqa: F401
    import isotp  # type: ignore  # noqa: F401
    _HAVE_CAN = True
except Exception:  # pragma: no cover - depends on local env
    _HAVE_CAN = False

from bmw_ecu.connection.base import TransportConfig, TransportKind
from bmw_ecu.connection.kdcan import KDCANTransport


def _cfg(tx: int, rx: int) -> TransportConfig:
    return TransportConfig(
        kind=TransportKind.KDCAN,
        channel="isotp-test",        # virtual bus channel name
        can_interface="virtual",     # python-can in-process loopback
        can_tx_id=tx,
        can_rx_id=rx,
        timeout=5.0,
    )


@unittest.skipUnless(_HAVE_CAN, "python-can + can-isotp not installed")
class KdcanIsoTpTests(unittest.TestCase):
    def test_requires_explicit_can_ids(self) -> None:
        # Safety rule: never guess CAN arbitration IDs.
        with self.assertRaises(ValueError):
            KDCANTransport(TransportConfig(
                kind=TransportKind.KDCAN, channel="x", can_interface="virtual",
            ))

    def test_multiframe_roundtrip(self) -> None:
        async def _run() -> bytes:
            tester = KDCANTransport(_cfg(tx=0x6F1, rx=0x612))
            ecu = KDCANTransport(_cfg(tx=0x612, rx=0x6F1))
            await tester.open()
            await ecu.open()
            try:
                # 64 bytes ⇒ forces FF + multiple CF + a Flow-Control frame.
                payload = bytes((i & 0xFF) for i in range(64))
                # Send and receive concurrently so Flow-Control can flow back
                # (both stacks must be pumped at the same time).
                send_task = asyncio.create_task(tester.send(0x612, payload))
                got = await ecu.recv(timeout=5.0)
                await send_task
                return got
            finally:
                await tester.close()
                await ecu.close()

        got = asyncio.run(_run())
        self.assertEqual(len(got), 64)
        self.assertEqual(got, bytes((i & 0xFF) for i in range(64)))

    def test_short_frame_roundtrip(self) -> None:
        async def _run() -> bytes:
            tester = KDCANTransport(_cfg(tx=0x6F1, rx=0x612))
            ecu = KDCANTransport(_cfg(tx=0x612, rx=0x6F1))
            await tester.open()
            await ecu.open()
            try:
                payload = bytes([0x62, 0xF1, 0x90])  # single frame (<=7 bytes)
                send_task = asyncio.create_task(tester.send(0x612, payload))
                got = await ecu.recv(timeout=5.0)
                await send_task
                return got
            finally:
                await tester.close()
                await ecu.close()

        self.assertEqual(asyncio.run(_run()), bytes([0x62, 0xF1, 0x90]))


if __name__ == "__main__":
    unittest.main()
