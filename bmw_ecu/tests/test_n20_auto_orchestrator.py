"""Dynamic Hardware Catalog + N20 Auto-Detect Orchestrator (Phase 3).

Proves the data-driven flow: the orchestrator reads the live Hardware ID over
UDS, looks up the EXACT board-revision profile, and yields a variant-specific
bench payload — two different N20 HW IDs must produce two different boot pins
+ images, with nothing hardcoded per ECU name.
"""
from __future__ import annotations

import asyncio
import unittest

from bmw_ecu.autodetect import (
    N20AutoOrchestrator,
    TprotStatus,
    get_hardware_profile,
)
from bmw_ecu.uds.client import UdsClient
from bmw_ecu.uds.services import BmwDID

from bmw_ecu.autodetect.n20_auto_orchestrator import TPROT_STATUS_DID


class _UdsBus:
    """Minimal UDS responder driven by a dict of DID → bytes.

    Returns positive RDBI frames [0x62, hi, lo, data...]; a missing DID yields
    NRC requestOutOfRange (0x31) so the orchestrator's try/except degrades.
    """

    def __init__(self, *, dids: dict[int, bytes], tprot: int | None) -> None:
        self.dids = dict(dids)
        if tprot is not None:
            self.dids[TPROT_STATUS_DID] = bytes([tprot])

    async def open(self) -> None:
        pass

    async def request(self, addr: int, payload: bytes, *, timeout: float = 5.0) -> bytes:
        sid = payload[0]
        if sid == 0x10:  # session control
            return bytes([0x50, payload[1]])
        if sid == 0x22:  # RDBI
            did = (payload[1] << 8) | payload[2]
            data = self.dids.get(did)
            if data is None:
                return bytes([0x7F, 0x22, 0x31])  # requestOutOfRange
            return bytes([0x62, payload[1], payload[2]]) + data
        return bytes([0x7F, sid, 0x11])

    async def recv(self, timeout=None) -> bytes:  # pragma: no cover
        raise AssertionError("recv not expected")


def _bus(hw_id: str, *, tprot: int | None, sw: str = "SW_TEST") -> _UdsBus:
    return _UdsBus(dids={
        BmwDID.HW_NUMBER: hw_id.encode("ascii"),
        BmwDID.SW_VERSION: sw.encode("ascii"),
    }, tprot=tprot)


def _run(bus: _UdsBus):
    client = UdsClient(bus, ecu_addr=0x12, session_name="t")
    return asyncio.run(N20AutoOrchestrator().run(client, vin="WBA_AUTO_0001"))


class CatalogTests(unittest.TestCase):
    def test_two_variants_have_distinct_boot_pins(self) -> None:
        rev_b = get_hardware_profile("8606229")
        rev_d = get_hardware_profile("8623136")
        self.assertIsNotNone(rev_b)
        self.assertIsNotNone(rev_d)
        self.assertNotEqual(rev_b.pinout.boot_pin, rev_d.pinout.boot_pin)
        self.assertNotEqual(rev_b.pinout.boot_image_url,
                            rev_d.pinout.boot_image_url)

    def test_unknown_id_returns_none(self) -> None:
        self.assertIsNone(get_hardware_profile("0000000"))


class ProbeTests(unittest.TestCase):
    def test_probe_reads_hardware_id_and_tprot(self) -> None:
        bus = _bus("8606229", tprot=0x01)
        client = UdsClient(bus, ecu_addr=0x12, session_name="t")
        probe = asyncio.run(N20AutoOrchestrator().probe(client))
        self.assertEqual(probe.hardware_id, "8606229")
        self.assertEqual(probe.tprot, TprotStatus.LOCKED)
        self.assertTrue(probe.locked)

    def test_missing_tprot_is_unknown_and_locked(self) -> None:
        bus = _bus("8606229", tprot=None)
        client = UdsClient(bus, ecu_addr=0x12, session_name="t")
        probe = asyncio.run(N20AutoOrchestrator().probe(client))
        self.assertEqual(probe.tprot, TprotStatus.UNKNOWN)
        self.assertTrue(probe.locked)  # conservative


class DecisionEngineTests(unittest.TestCase):
    def test_unlocked_yields_obd_flow(self) -> None:
        payload = _run(_bus("8606229", tprot=0x00))
        self.assertEqual(payload.diagnostics["flow"], "obd_direct")
        self.assertEqual(payload.required_action, "load_features")

    def test_locked_known_hw_yields_variant_specific_bench(self) -> None:
        payload = _run(_bus("8606229", tprot=0x01))
        d = payload.diagnostics
        self.assertEqual(d["flow"], "bench")
        self.assertEqual(payload.required_action, "confirm_bench_wiring")
        self.assertEqual(d["hardware"]["board_revision"], "Rev B (N20 pre-LCI)")
        # The locate step must carry THIS board's boot pin + image.
        locate = next(s for s in d["steps"] if s["kind"] == "locate")
        self.assertIn("24", locate["ar"])
        self.assertTrue(locate["image_url"].endswith("8606229_boot.jpg"))
        # Sequence ends with the bench_extract → code_ecu actions.
        actions = [s["action"] for s in d["steps"] if s["kind"] == "action"]
        self.assertEqual(actions, ["bench_extract", "code_ecu"])

    def test_different_hw_id_changes_the_payload(self) -> None:
        rev_b = _run(_bus("8606229", tprot=0x01)).diagnostics
        rev_d = _run(_bus("8623136", tprot=0x01)).diagnostics
        b_locate = next(s for s in rev_b["steps"] if s["kind"] == "locate")
        d_locate = next(s for s in rev_d["steps"] if s["kind"] == "locate")
        self.assertIn("24", b_locate["ar"])     # Rev B boot pin
        self.assertIn("31", d_locate["ar"])     # Rev D boot pin (moved!)
        self.assertNotEqual(b_locate["image_url"], d_locate["image_url"])
        self.assertNotEqual(rev_b["hardware"]["board_revision"],
                            rev_d["hardware"]["board_revision"])

    def test_locked_unknown_hw_refuses_to_guess(self) -> None:
        payload = _run(_bus("9999999", tprot=0x01))
        self.assertEqual(payload.diagnostics["flow"], "unknown_hardware")
        self.assertEqual(payload.required_action, "report_hardware_id")
        # No fabricated steps for hardware we don't know.
        self.assertNotIn("steps", payload.diagnostics)

    def test_wiring_step_carries_dynamic_pins(self) -> None:
        d = _run(_bus("8606229", tprot=0x01)).diagnostics
        wiring = next(s for s in d["steps"] if s["kind"] == "wiring")
        pins = {w["function"]: w["ecu_pin"] for w in wiring["wires"]}
        self.assertEqual(pins["power"], 87)
        self.assertEqual(pins["ground"], 88)
        self.assertEqual(pins["k_line"], 63)


if __name__ == "__main__":
    unittest.main()
