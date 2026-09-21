"""
Pure-Python tests for the robot's retail-only pricing guard.

These deliberately avoid the Django DB so they run anywhere (the guarantee they
protect — "the robot never sees wholesale/cost" — must be verifiable in isolation
and can't depend on a Postgres/tenant setup). They use a duck-typed product.

Run: python -m pytest robot/tests/test_pricing_guard.py
     (or) python -m unittest robot.tests.test_pricing_guard
"""

import types
import unittest
from decimal import Decimal

from robot import pricing, services


def _product(**over):
    """A stand-in for inventory.Product with both retail and forbidden fields."""
    base = dict(
        id=1,
        name="Front Control Arm (Left)",
        part_number="31126794339",
        barcode="8801234567890",
        oem_cross_reference=["31126765993"],
        all_part_numbers=["31126794339", "31126765993"],
        car_model="BMW F30",
        retail_price=Decimal("1850.00"),
        scrap_price=Decimal("300.00"),
        ai_suggested_price=Decimal("1600.00"),
        warranty_months=12,
        total_stock=7,
        # Forbidden fields — MUST NEVER surface in a robot payload:
        b2b_wholesale_price=Decimal("1200.00"),
        purchase_price=Decimal("950.00"),
        average_cost=Decimal("980.00"),
    )
    base.update(over)
    return types.SimpleNamespace(**base)


class RetailOnlyGuardTests(unittest.TestCase):
    def test_payload_exposes_retail_only(self):
        payload = pricing.safe_product_payload(_product())
        self.assertTrue(payload["retail_price"] == 1850.0)
        self.assertEqual(payload["stock"], 7)
        # The whole point: none of the forbidden keys are present.
        for forbidden in pricing.FORBIDDEN_PRICE_FIELDS:
            self.assertNotIn(forbidden, payload)

    def test_no_wholesale_value_anywhere_in_payload(self):
        payload = pricing.safe_product_payload(_product(), include_scrap=True)
        # No value in the payload equals the wholesale/cost figures.
        forbidden_values = {1200.0, 950.0, 980.0}
        for key, val in payload.items():
            if isinstance(val, (int, float)):
                self.assertNotIn(float(val), forbidden_values, f"leaked via {key}")

    def test_scrap_flow_adds_retail_scrap_fields_only(self):
        payload = pricing.safe_product_payload(_product(), include_scrap=True)
        self.assertIn("scrap_price", payload)
        self.assertIn("ai_suggested_price", payload)
        self.assertNotIn("b2b_wholesale_price", payload)

    def test_tripwire_raises_if_forbidden_field_injected(self):
        # Simulate a future regression that copies a forbidden field in.
        bad = {"retail_price": 10.0, "b2b_wholesale_price": 5.0}
        with self.assertRaises(AssertionError):
            pricing._assert_no_wholesale(bad)

    def test_redact_strips_wholesale_mentions(self):
        self.assertEqual(pricing.redact("normal retail reply"), "normal retail reply")
        self.assertNotIn("wholesale", pricing.redact("the wholesale price is 1200").lower())
        self.assertNotIn("جمله", pricing.redact("سعر الجمله 1200"))


class DynamicScrapPricingTests(unittest.TestCase):
    """suggest_used_price interpolates between scrap floor and retail/AI ceiling."""

    def test_like_new_approaches_ceiling(self):
        p = _product()  # scrap 300, ai 1600 → ceiling = ai = 1600
        self.assertEqual(services.suggest_used_price(p, 1.0), Decimal("1600.00"))

    def test_destroyed_hits_floor(self):
        p = _product()
        self.assertEqual(services.suggest_used_price(p, 0.0), Decimal("300.00"))

    def test_midpoint_is_between_floor_and_ceiling(self):
        p = _product()
        mid = services.suggest_used_price(p, 0.5)
        self.assertEqual(mid, Decimal("950.00"))  # 300 + 0.5*(1600-300)

    def test_ceiling_falls_back_to_retail_without_ai_price(self):
        p = _product(ai_suggested_price=Decimal("0"))  # ceiling = retail 1850
        self.assertEqual(services.suggest_used_price(p, 1.0), Decimal("1850.00"))

    def test_score_is_clamped(self):
        p = _product()
        self.assertEqual(services.suggest_used_price(p, 5.0), Decimal("1600.00"))
        self.assertEqual(services.suggest_used_price(p, -3.0), Decimal("300.00"))


class LearningKeyTests(unittest.TestCase):
    """The learning memory normalizes keys so the same code/label matches again."""

    def test_normalize_key_is_case_and_space_insensitive(self):
        self.assertEqual(services._normalize_key("  BMW  F30 "), "bmw f30")
        self.assertEqual(services._normalize_key("31126794339"), "31126794339")
        self.assertEqual(services._normalize_key(""), "")
        self.assertEqual(services._normalize_key(None), "")


if __name__ == "__main__":
    unittest.main()
