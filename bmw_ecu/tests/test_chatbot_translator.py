"""Translator maps known exceptions to actionable chatbot payloads."""
from __future__ import annotations

import unittest

from bmw_ecu.exceptions import (
    CANLinesReversedError, ECUNoPowerError, FlashRollbackFailed,
    IgnitionOffError, LowVoltage, UdsNegativeResponse,
)
from bmw_ecu.execution.base import StrategyOutcome, StrategyResult
from bmw_ecu.services.chatbot_translator import (
    translate_exception, translate_result,
)


class TranslateExceptionTests(unittest.TestCase):
    def test_no_power_includes_required_action(self) -> None:
        p = translate_exception(ECUNoPowerError("no 12V"))
        self.assertEqual(p.required_action, "verify_power")
        self.assertEqual(p.severity, "error")
        self.assertIn("12V", p.chatbot_message)

    def test_can_reversed(self) -> None:
        p = translate_exception(CANLinesReversedError())
        self.assertEqual(p.required_action, "swap_can_lines")

    def test_ignition_off_is_warning(self) -> None:
        p = translate_exception(IgnitionOffError())
        self.assertEqual(p.severity, "warning")
        self.assertEqual(p.required_action, "verify_ignition")

    def test_low_voltage_carries_volts_context(self) -> None:
        p = translate_exception(LowVoltage("11.9V too low", volts=11.9))
        self.assertIn("11.9", p.chatbot_message)
        self.assertEqual(p.required_action, "connect_charger")

    def test_uds_nrc_surfaces_codes(self) -> None:
        p = translate_exception(UdsNegativeResponse(0x27, 0x35))
        self.assertEqual(p.diagnostics["nrc"], 0x35)

    def test_rollback_failed_is_critical(self) -> None:
        p = translate_exception(FlashRollbackFailed("rollback failed"))
        self.assertEqual(p.severity, "critical")
        self.assertEqual(p.required_action, "halt_and_call_senior")

    def test_unknown_exception_falls_back_safely(self) -> None:
        p = translate_exception(ValueError("totally novel"))
        self.assertEqual(p.severity, "critical")
        self.assertIn("class", p.diagnostics)


class TranslateResultTests(unittest.TestCase):
    def test_success(self) -> None:
        r = StrategyResult(outcome=StrategyOutcome.SUCCESS,
                           strategy_name="software_only",
                           backup_sha256="a" * 64)
        p = translate_result(r)
        self.assertEqual(p.required_action, "session_complete")

    def test_suspended_carries_visual_aid(self) -> None:
        r = StrategyResult(
            outcome=StrategyOutcome.SUSPENDED,
            strategy_name="interactive_guided",
            wizard_next_step={"session_id": 42,
                              "step": {"kind": "show_pinout",
                                       "instructions": "tap continue",
                                       "pinout_diagram_url": "/static/x.svg"}},
        )
        p = translate_result(r)
        self.assertEqual(p.required_action, "show_pinout")
        self.assertEqual(p.visual_aid_url, "/static/x.svg")
        self.assertEqual(p.diagnostics["wizard_session_id"], 42)

    def test_rolled_back(self) -> None:
        r = StrategyResult(outcome=StrategyOutcome.FAILED_ROLLED_BACK,
                           strategy_name="hardware_automation",
                           error_code="HW_INJECT_FAILED")
        p = translate_result(r)
        self.assertEqual(p.severity, "warning")
        self.assertEqual(p.required_action, "retry_with_fallback")
