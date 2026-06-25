"""Selection-rule tests for ExecutionStrategyManager.

We don't need real transports — the Manager only inspects EcuProfile +
WorkshopCapabilities. Strategies are dummy ExecutionStrategy instances.
"""
from __future__ import annotations

import asyncio
import unittest
from dataclasses import replace

from bmw_ecu.execution import (
    ExecutionStrategyManager,
    KNOWN_PROFILES,
    WorkshopCapabilities,
)
from bmw_ecu.execution.base import (
    ExecutionStrategy, StrategyContext, StrategyOutcome, StrategyResult,
)
from bmw_ecu.execution.manager import NoStrategyAvailable


class _DummyStrategy(ExecutionStrategy):
    def __init__(self, name: str, **flags) -> None:
        self.name = name
        for k, v in flags.items():
            setattr(self, k, v)

    async def extract_isn(self, ctx):
        return StrategyResult(outcome=StrategyOutcome.SUCCESS, strategy_name=self.name)

    async def inject_isn(self, ctx):
        return StrategyResult(outcome=StrategyOutcome.SUCCESS, strategy_name=self.name)

    async def rollback(self, ctx, *, reason: str):
        return StrategyResult(outcome=StrategyOutcome.FAILED_ROLLED_BACK,
                              strategy_name=self.name, error_message=reason)


def _ctx(profile, caps):
    return StrategyContext(
        vin="WBA0000TEST00000", profile=profile, capabilities=caps,
        transport=None, security=None, preflight=None,  # type: ignore[arg-type]
    )


class ManagerSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sw = _DummyStrategy("software_only", requires_software_capable=True)
        self.hw = _DummyStrategy("hardware_automation", requires_hardware_box=True)
        self.wz = _DummyStrategy("interactive_guided", requires_technician=True)
        self.mgr = ExecutionStrategyManager(self.sw, self.hw, self.wz)

    def test_mevd17_requires_bench_and_no_probe_falls_to_wizard(self) -> None:
        profile = KNOWN_PROFILES["MEVD17_2_9"]
        caps = WorkshopCapabilities(has_enet_cable=True, technician_skill_level=3)
        choice = self.mgr.select(_ctx(profile, caps))
        self.assertEqual(choice.primary.name, "interactive_guided")

    def test_software_preferred_for_pre_2014_fem(self) -> None:
        profile = KNOWN_PROFILES["FEM_F30"]
        caps = WorkshopCapabilities(has_enet_cable=True, has_smart_breakout_box=True,
                                    technician_skill_level=3)
        choice = self.mgr.select(_ctx(profile, caps))
        # Pre-2014 FEM is MEDIUM protection with a known exploit → software wins.
        self.assertEqual(choice.primary.name, "software_only")
        # Hardware + wizard remain as fallbacks.
        self.assertIn("hardware_automation", [s.name for s in choice.fallbacks])

    def test_high_protection_no_probe_routes_to_wizard_first(self) -> None:
        profile = KNOWN_PROFILES["FEM_F30_POST_2014"]
        caps = WorkshopCapabilities(has_enet_cable=True, has_smart_breakout_box=True,
                                    technician_skill_level=3)
        choice = self.mgr.select(_ctx(profile, caps))
        # HIGH protection + no BDM probe → wizard floats to front.
        self.assertEqual(choice.primary.name, "interactive_guided")

    def test_no_capabilities_raises(self) -> None:
        profile = KNOWN_PROFILES["FEM_F30"]
        caps = WorkshopCapabilities(has_enet_cable=False, has_kdcan_cable=False,
                                    technician_skill_level=1)
        with self.assertRaises(NoStrategyAvailable):
            self.mgr.select(_ctx(profile, caps))

    def test_hardware_chosen_when_no_exploit_no_wizard(self) -> None:
        profile = replace(KNOWN_PROFILES["FEM_F30_POST_2014"],
                          requires_bench=False)
        caps = WorkshopCapabilities(has_enet_cable=True, has_smart_breakout_box=True,
                                    technician_skill_level=1)  # junior → no wizard
        choice = self.mgr.select(_ctx(profile, caps))
        self.assertEqual(choice.primary.name, "hardware_automation")


class _FakeHAL:
    """Minimal stand-in for SmartBoxHAL — records calls."""
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.opened = False

    async def open(self) -> None: self.opened = True; self.calls.append("open")
    async def close(self) -> None: self.calls.append("close")
    async def set_pin(self, pin, rail) -> None: self.calls.append(f"set:{pin}:{rail.value}")
    async def read_voltage(self, pin) -> float: return 12.0
    async def all_off(self) -> None: self.calls.append("all_off")
    async def __aenter__(self): await self.open(); return self
    async def __aexit__(self, *exc): await self.all_off(); await self.close()


class GlitchSequenceTests(unittest.TestCase):
    def test_standard_bsl_runs_in_order(self) -> None:
        from bmw_ecu.execution.hardware_automation.boot_pin_sequencer import (
            run_sequence, standard_bsl_sequence,
        )
        from bmw_ecu.execution.hardware_automation.pin_maps import lookup

        async def run() -> None:
            hal = _FakeHAL()
            await hal.open()
            await run_sequence(hal, standard_bsl_sequence(lookup("FEM_F30")))
            await hal.close()
            pin_calls = [c for c in hal.calls if c.startswith("set:")]
            # 4 steps in standard_bsl_sequence
            self.assertEqual(len(pin_calls), 4)
            # First call grounds GND pin
            self.assertTrue(pin_calls[0].endswith("gnd"))

        asyncio.run(run())


class WizardStateMachineTests(unittest.TestCase):
    def test_legal_transitions(self) -> None:
        from bmw_ecu.execution.interactive_guided.state_machine import (
            WizardState, WizardStateMachine, IllegalTransition,
        )
        sm = WizardStateMachine()
        sm.advance(WizardState.SHOWING_PINOUT)
        sm.advance(WizardState.AWAITING_POWER)
        sm.advance(WizardState.AWAITING_GLITCH)
        sm.advance(WizardState.AWAITING_ISN)
        sm.advance(WizardState.INJECTING)
        sm.advance(WizardState.DONE)
        self.assertTrue(sm.is_terminal)

        sm2 = WizardStateMachine()
        with self.assertRaises(IllegalTransition):
            sm2.advance(WizardState.INJECTING)
