"""Guided Connect & Read — the real-world coding workflow.

Covers:
  • assess_connection() decides OPEN vs LOCKED off the EcuProfile.
  • OPEN modules (FEM_F30, software exploit) → straight-to-code steps,
    cable="enet", no scary bench teardown.
  • LOCKED modules (MEVD17_2_9 / N20, bench-only) → full bench procedure
    with pinout diagram + coloured callouts + boot-pin step, cable="dcan_bench".
  • Pin numbers in the locked procedure are pulled from the live pinout
    (so the steps always match the real connector).
  • CodingOrchestrator action="connect_read" returns the guidance in the
    standard ChatbotPayload JSON shape, with outcome connected/module_locked.
"""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from bmw_ecu.coding.guided_connect import assess_connection
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

    def test_steps_include_removal_power_dcan_boot_and_photo(self) -> None:
        ars = [s.ar for s in self.a.steps]
        joined = " ".join(ars)
        self.assertGreaterEqual(len(ars), 5)
        self.assertIn("فك الكنترول", joined)      # remove module
        self.assertIn("12V", joined)               # power
        self.assertIn("D-CAN", joined)             # diagnostic cable
        self.assertIn("BSL", " ".join(s.en for s in self.a.steps))  # bootloader
        self.assertIn("📸", joined)                # photograph PCB

    def test_boot_pin_from_profile_appears_in_steps(self) -> None:
        # MEVD17_2_9.boot_pin == 24 — must surface in the BSL step.
        boot_step = next(s for s in self.a.steps if "BSL" in s.en)
        self.assertIn("24", boot_step.en)

    def test_power_pins_pulled_from_pinout(self) -> None:
        # MEVD17_2_9 pinout: 12V on pin 87, GND on pin 88.
        power_step = next(s for s in self.a.steps if "12V" in s.ar)
        self.assertIn("87", power_step.ar)
        self.assertIn("88", power_step.ar)


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

    def test_locked_module_returns_module_locked_with_pinout(self) -> None:
        body = self._run("MEVD17_2_9")
        self.assertEqual(body["outcome"], "module_locked")
        self.assertTrue(body["diagnostics"]["locked"])
        self.assertEqual(body["diagnostics"]["cable"], "dcan_bench")
        self.assertTrue(body["visual_aid_url"])
        g = body["diagnostics"]["guidance"]
        self.assertTrue(g["pinout_callouts"])
        self.assertGreaterEqual(len(g["steps"]), 5)


if __name__ == "__main__":
    unittest.main()
