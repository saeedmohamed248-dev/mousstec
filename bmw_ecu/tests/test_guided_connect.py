"""Guided Connect & Read — the real-world coding workflow.

Covers:
  • assess_connection() decides OPEN vs LOCKED off the EcuProfile.
  • OPEN modules (FEM_F30, software exploit) → straight-to-code steps,
    cable="enet", no scary bench teardown.
  • LOCKED modules (MEVD17_2_9 / N20, bench-only) → full wire-by-wire bench
    procedure: connect D-CAN to laptop, then OBD-pin → ECU-pin wiring map,
    boot-pin step, cable="dcan_bench".
  • The wiring map is built from the standard OBD-II (J1962) plug on one side
    and the live ECU pinout on the other, so the steps always match the real
    connector.
  • CodingOrchestrator action="connect_read" returns the guidance in the
    standard ChatbotPayload JSON shape, with outcome connected/module_locked.
"""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from bmw_ecu.coding.guided_connect import (
    LiveIdentifier, LiveRead, assess_connection, read_live,
)
from bmw_ecu.execution.ecu_profiles import KNOWN_PROFILES


class AssessOpenModuleTests(unittest.TestCase):
    def test_fem_f30_is_open_and_codes_over_enet(self) -> None:
        profile = KNOWN_PROFILES["FEM_F30"]  # MEDIUM + known exploit → OPEN
        a = asyncio.run(assess_connection(
            profile=profile, vin="WBA3A5C50DF000001", chassis="F30"))
        self.assertFalse(a.locked)
        self.assertEqual(a.cable, "enet")
        self.assertEqual(a.ecu_name, "FEM_F30")
        self.assertIn("مفتوح", a.headline_ar)
        # OPEN module has no bench wiring map.
        self.assertEqual(a.wiring, [])
        # OPEN procedure is short + does NOT tell them to rip the module out.
        joined = " ".join(s.ar for s in a.steps)
        self.assertNotIn("فك الكنترول", joined)
        self.assertTrue(any("Load features" in s.en for s in a.steps))


class AssessLockedModuleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.profile = KNOWN_PROFILES["MEVD17_2_9"]  # HIGH + bench → LOCKED
        self.a = asyncio.run(assess_connection(
            profile=self.profile, vin="WBA3A5C50DF000002", chassis="F30"))

    def test_locked_with_bench_dcan_cable(self) -> None:
        self.assertTrue(self.a.locked)
        self.assertEqual(self.a.cable, "dcan_bench")
        self.assertEqual(self.a.protection, "HIGH")

    def test_has_pinout_diagram_and_callouts(self) -> None:
        self.assertTrue(self.a.pinout_diagram_url)
        self.assertTrue(self.a.pinout_callouts)

    def test_steps_connect_dcan_then_remove_boot_and_diagram(self) -> None:
        ars = [s.ar for s in self.a.steps]
        joined = " ".join(ars)
        ens = " ".join(s.en for s in self.a.steps)
        self.assertGreaterEqual(len(ars), 5)
        self.assertIn("فك الكنترول", joined)       # remove module
        # The tech is told to connect the D-CAN interface to the laptop first.
        self.assertIn("D-CAN", joined)
        self.assertIn("laptop", ens)
        self.assertIn("BSL", ens)                  # bootloader step
        # The system tells him to read the coloured diagram.
        self.assertIn("المخطط", joined)

    def test_wiring_map_is_obd_to_ecu(self) -> None:
        # N20 DME: 12V on ECU pin 87, GND on ECU pin 88, K-Line on ECU pin 63.
        # Standard OBD-II side: 12V=16, GND=4, K-Line=7.
        by_fn = {w.function: w for w in self.a.wiring}
        self.assertIn("12v", by_fn)
        self.assertEqual(by_fn["12v"].obd_pin, 16)
        self.assertEqual(by_fn["12v"].ecu_pin, 87)
        self.assertIn("gnd", by_fn)
        self.assertEqual(by_fn["gnd"].obd_pin, 4)
        self.assertEqual(by_fn["gnd"].ecu_pin, 88)
        # N20 talks K-Line, not CAN.
        self.assertIn("kline", by_fn)
        self.assertEqual(by_fn["kline"].obd_pin, 7)
        self.assertEqual(by_fn["kline"].ecu_pin, 63)
        self.assertNotIn("canh", by_fn)

    def test_kline_wire_step_shows_both_pins(self) -> None:
        # The per-wire step must spell out OBD pin 7 → ECU pin 63.
        kline_step = next(s for s in self.a.steps if "K-Line" in s.en)
        self.assertIn("7", kline_step.en)
        self.assertIn("63", kline_step.en)

    def test_boot_pin_from_profile_appears_in_bsl_step(self) -> None:
        # MEVD17_2_9.boot_pin == 24 — must surface in the BSL step.
        self.assertEqual(self.a.boot_pin, 24)
        boot_step = next(s for s in self.a.steps if "BSL" in s.en)
        self.assertIn("24", boot_step.en)


class AssessLockedCanModuleTests(unittest.TestCase):
    """FEM_F30_POST_2014 is LOCKED (HIGH, no exploit) and shares the FEM_F30
    connector — the diagram falls back to the base, giving a CAN-based bench
    procedure (proves the wiring map adapts the other way)."""
    def setUp(self) -> None:
        self.a = asyncio.run(assess_connection(
            profile=KNOWN_PROFILES["FEM_F30_POST_2014"],
            vin="WBA3A5C50DF000099", chassis="F30"))

    def test_locked(self) -> None:
        self.assertTrue(self.a.locked)

    def test_uses_base_diagram_and_can_wiring(self) -> None:
        # Fallback to FEM_F30 diagram → has CAN pins → CAN-H/CAN-L wires.
        self.assertTrue(self.a.pinout_callouts)
        by_fn = {w.function: w for w in self.a.wiring}
        self.assertIn("canh", by_fn)
        self.assertEqual(by_fn["canh"].obd_pin, 6)   # OBD CAN-H
        self.assertEqual(by_fn["canh"].ecu_pin, 15)  # FEM CAN-H
        self.assertIn("canl", by_fn)
        self.assertEqual(by_fn["canl"].obd_pin, 14)  # OBD CAN-L
        self.assertEqual(by_fn["canl"].ecu_pin, 16)  # FEM CAN-L
        self.assertNotIn("kline", by_fn)
        # A CAN module gets the "don't reverse the pair" safety step.
        joined = " ".join(s.ar for s in self.a.steps)
        self.assertIn("CAN", joined)


# --- Real live read --------------------------------------------------------
class ReadLiveTests(unittest.TestCase):
    """read_live() actually talks UDS to the (mock) ECU and collects data."""
    def _client(self, ecu):
        from bmw_ecu.mocks import MockTransport
        from bmw_ecu.uds import UdsClient

        async def go():
            transport = MockTransport(ecu)
            await transport.open()
            return await read_live(
                UdsClient(transport, ecu_addr=0x40, session_name="t"))
        return asyncio.run(go())

    def test_reachable_ecu_returns_vin_and_battery(self) -> None:
        from bmw_ecu.mocks import MockEcu
        live = self._client(MockEcu(vin="WBA3A5C50DF000077", battery_volts=13.4))
        self.assertTrue(live.reachable)
        self.assertTrue(live.session_ok)
        by_did = {i.did: i for i in live.identifiers}
        self.assertIn(0xF190, by_did)
        self.assertEqual(by_did[0xF190].value, "WBA3A5C50DF000077")
        self.assertIn(0xF40C, by_did)
        self.assertEqual(by_did[0xF40C].value, "13.4 V")

    def test_dead_ecu_is_not_reachable(self) -> None:
        # A transport whose ECU raises on every request → nothing answers.
        class DeadTransport:
            async def open(self): pass
            async def request(self, *a, **k): raise TimeoutError("no reply")
            async def recv(self, *a, **k): raise TimeoutError("no reply")

        from bmw_ecu.uds import UdsClient

        async def go():
            return await read_live(
                UdsClient(DeadTransport(), ecu_addr=0x40, session_name="t"))
        live = asyncio.run(go())
        self.assertFalse(live.reachable)
        self.assertFalse(live.session_ok)
        self.assertEqual(live.identifiers, [])


class AssessWithLiveReadTests(unittest.TestCase):
    def test_not_reachable_gives_not_connected_verdict(self) -> None:
        live = LiveRead(reachable=False)
        a = asyncio.run(assess_connection(
            profile=KNOWN_PROFILES["FEM_F30"], vin="WBA0", chassis="F30",
            live=live))
        self.assertFalse(a.reachable)
        # No fake open/locked claim; steps tell the tech to check the cable.
        joined = " ".join(s.ar for s in a.steps)
        self.assertIn("الكونتاكت", joined)

    def test_reachable_surfaces_real_identifiers_and_live_vin(self) -> None:
        live = LiveRead(reachable=True, session_ok=True, identifiers=[
            LiveIdentifier(0xF190, "VIN", "VIN", "WBA_LIVE_VIN_001", "00"),
            LiveIdentifier(0xF195, "SW", "Software version", "SW_42", "01"),
        ])
        a = asyncio.run(assess_connection(
            profile=KNOWN_PROFILES["FEM_F30"], vin="SESSION_VIN", chassis="F30",
            live=live))
        self.assertTrue(a.reachable)
        # Live VIN wins over the session VIN.
        self.assertEqual(a.vin, "WBA_LIVE_VIN_001")
        self.assertEqual(len(a.identifiers), 2)
        self.assertEqual(a.identifiers[0]["did"], "0xF190")


# --- Orchestrator integration (no DB) — stub the billing gate ---------------
class _StubGate:
    """Minimal AbstractBillingGate stand-in: always entitled, no DB."""
    async def verify_coding_subscription_or_hold(self, *, vin, operation_type="coding"):
        from bmw_ecu.services.billing_gate import CodingEntitlement
        return CodingEntitlement(
            entitled=True, operation_type="coding", mode="subscription",
            subscription_ref="stub")


class ConnectReadOrchestratorTests(unittest.TestCase):
    def _ctx(self, profile_name: str):
        from bmw_ecu.execution.base import StrategyContext
        from bmw_ecu.execution.capabilities import WorkshopCapabilities
        from bmw_ecu.mocks import MockEcu, MockTransport
        from bmw_ecu.safety import BackupStore, BatteryMonitor, PreflightGate
        from bmw_ecu.uds import MockSeedKeyProvider, SecurityAccess, UdsClient

        profile = KNOWN_PROFILES[profile_name]
        ecu = MockEcu(vin="WBA3A5C50DF000003")
        transport = MockTransport(ecu)
        asyncio.get_event_loop()
        client = UdsClient(transport, ecu_addr=profile.uds_isn_did >> 8,
                           session_name="t")
        security = SecurityAccess(client, MockSeedKeyProvider())
        self._tmp = tempfile.TemporaryDirectory()
        store = BackupStore(Path(self._tmp.name))

        async def v() -> float:
            return 13.6
        preflight = PreflightGate(BatteryMonitor(reader=v), store)
        return StrategyContext(
            vin="WBA3A5C50DF000003", profile=profile,
            capabilities=WorkshopCapabilities(),
            transport=transport, security=security, preflight=preflight)

    def _run(self, profile_name: str):
        from bmw_ecu.api.coding_orchestrator import CodingOrchestrator

        async def go():
            ctx = self._ctx(profile_name)
            await ctx.transport.open()
            orch = CodingOrchestrator(billing=_StubGate())
            resp = await orch.run(
                ctx=ctx, coding_request={"action": "connect_read", "chassis": "F30"})
            return resp.to_json()
        return asyncio.run(go())

    def test_open_module_returns_connected(self) -> None:
        body = self._run("FEM_F30")
        self.assertEqual(body["outcome"], "connected")
        self.assertFalse(body["diagnostics"]["locked"])
        self.assertEqual(body["diagnostics"]["cable"], "enet")
        self.assertIn("steps", body["diagnostics"]["guidance"])

    def test_reachable_module_surfaces_live_identifiers(self) -> None:
        body = self._run("FEM_F30")
        self.assertEqual(body["outcome"], "connected")
        self.assertTrue(body["diagnostics"]["reachable"])
        # The MockEcu answers VIN + battery → real data surfaces.
        idents = body["diagnostics"]["identifiers"]
        self.assertTrue(idents)
        dids = {i["did"] for i in idents}
        self.assertIn("0xF190", dids)
        self.assertIn("📟", body["chatbot_message"])

    def test_dead_ecu_returns_not_connected(self) -> None:
        from bmw_ecu.api.coding_orchestrator import CodingOrchestrator

        class DeadTransport:
            async def open(self): pass
            async def request(self, *a, **k): raise TimeoutError("no reply")
            async def recv(self, *a, **k): raise TimeoutError("no reply")

        async def go():
            ctx = self._ctx("FEM_F30")
            ctx.transport = DeadTransport()
            orch = CodingOrchestrator(billing=_StubGate())
            resp = await orch.run(
                ctx=ctx, coding_request={"action": "connect_read", "chassis": "F30"})
            return resp.to_json()

        body = asyncio.run(go())
        self.assertEqual(body["outcome"], "not_connected")
        self.assertFalse(body["diagnostics"]["reachable"])

    def test_locked_module_returns_module_locked_with_wiring(self) -> None:
        body = self._run("MEVD17_2_9")
        self.assertEqual(body["outcome"], "module_locked")
        self.assertTrue(body["diagnostics"]["locked"])
        self.assertEqual(body["diagnostics"]["cable"], "dcan_bench")
        self.assertTrue(body["visual_aid_url"])
        g = body["diagnostics"]["guidance"]
        self.assertTrue(g["pinout_callouts"])
        self.assertGreaterEqual(len(g["steps"]), 5)
        # The wiring map serializes as OBD-pin → ECU-pin rows.
        self.assertTrue(g["wiring"])
        w0 = g["wiring"][0]
        self.assertIn("obd_pin", w0)
        self.assertIn("ecu_pin", w0)


if __name__ == "__main__":
    unittest.main()
