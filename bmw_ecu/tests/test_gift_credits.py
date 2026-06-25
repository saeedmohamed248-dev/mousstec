"""GiftCredit model semantics — no DB needed.

The async consume_gift / has_active_gift paths are exercised against
the live tenant DB in the integration test suite (mousstec runs those
in the staging pipeline). Here we cover the pure model logic +
SettlementOutcome wiring.
"""
from __future__ import annotations

import unittest
from dataclasses import asdict
from datetime import timedelta
from decimal import Decimal

from bmw_ecu.services.settlement import SettlementOutcome


class SettlementOutcomeGiftFieldsTests(unittest.TestCase):
    def test_gift_outcome_carries_pk_and_remaining(self) -> None:
        out = SettlementOutcome(
            mode="gift", succeeded=True, amount=Decimal("450"),
            gift_pk=42, gift_grant_type="coding_credits",
            gift_remaining_after=4,
        )
        self.assertEqual(out.mode, "gift")
        self.assertTrue(out.succeeded)
        self.assertEqual(out.gift_pk, 42)
        self.assertEqual(out.gift_remaining_after, 4)
        self.assertEqual(out.wallet_before, None)

    def test_failed_outcome_carries_no_gift(self) -> None:
        out = SettlementOutcome(
            mode="failed", succeeded=False, amount=Decimal("0"),
            error_message="no path",
        )
        self.assertEqual(out.gift_pk, None)
        self.assertEqual(out.gift_grant_type, "")

    def test_outcome_dict_shape(self) -> None:
        # `asdict` exercises every field — guards against typo regressions
        # in the dataclass declaration.
        d = asdict(SettlementOutcome(
            mode="gift", succeeded=True, amount=Decimal("450"),
            gift_pk=1, gift_grant_type="subscription_window",
            gift_remaining_after=None,
        ))
        for key in ("mode", "succeeded", "amount", "wallet_before",
                    "wallet_after", "paymob_iframe_url", "gift_pk",
                    "gift_grant_type", "gift_remaining_after",
                    "error_message"):
            self.assertIn(key, d)


class GiftBundleConstantsTests(unittest.TestCase):
    def test_isn_and_coding_bundles_include_subscription(self) -> None:
        from bmw_ecu.services.gift_credits import (
            GIFT_TYPES_FOR_CODING, GIFT_TYPES_FOR_ISN,
        )
        # subscription_window grants entitlement for both flows.
        self.assertIn("subscription_window", GIFT_TYPES_FOR_ISN)
        self.assertIn("subscription_window", GIFT_TYPES_FOR_CODING)
        # Counted credits are separated per flow so a coding pack
        # doesn't accidentally settle an ISN charge.
        self.assertIn("isn_credits", GIFT_TYPES_FOR_ISN)
        self.assertNotIn("coding_credits", GIFT_TYPES_FOR_ISN)
        self.assertIn("coding_credits", GIFT_TYPES_FOR_CODING)
        self.assertNotIn("isn_credits", GIFT_TYPES_FOR_CODING)
