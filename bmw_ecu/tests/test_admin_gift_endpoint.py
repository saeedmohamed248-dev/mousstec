"""Validation + payload shape for POST /api/admin/entitlements/gift.

We test the request-validator pure function directly so we don't need
DRF + tenant DB just to exercise the validation matrix.
"""
from __future__ import annotations

import unittest

from bmw_ecu.api._admin_validation import validate_gift_payload as _validate


class AdminGiftValidatorTests(unittest.TestCase):
    def test_tenant_schema_required(self) -> None:
        err = _validate({"grant_type": "coding_credits", "credits": 5})
        self.assertIn("tenant_schema", err)

    def test_grant_type_must_be_valid(self) -> None:
        err = _validate({"tenant_schema": "ws_a", "grant_type": "bogus"})
        self.assertIn("grant_type", err)

    def test_subscription_requires_valid_until(self) -> None:
        err = _validate({"tenant_schema": "ws_a",
                         "grant_type": "subscription_window"})
        self.assertIn("valid_until", err)

    def test_subscription_with_valid_until_passes(self) -> None:
        err = _validate({"tenant_schema": "ws_a",
                         "grant_type": "subscription_window",
                         "valid_until": "2026-12-31T23:59"})
        self.assertEqual(err, "")

    def test_coding_credits_requires_positive_count(self) -> None:
        err = _validate({"tenant_schema": "ws_a",
                         "grant_type": "coding_credits", "credits": 0})
        self.assertIn("credits", err)

    def test_coding_credits_negative_rejected(self) -> None:
        err = _validate({"tenant_schema": "ws_a",
                         "grant_type": "coding_credits", "credits": -3})
        self.assertIn("credits", err)

    def test_coding_credits_non_int_rejected(self) -> None:
        err = _validate({"tenant_schema": "ws_a",
                         "grant_type": "coding_credits", "credits": "five"})
        self.assertIn("credits", err)

    def test_coding_credits_positive_passes(self) -> None:
        err = _validate({"tenant_schema": "ws_a",
                         "grant_type": "coding_credits", "credits": 5})
        self.assertEqual(err, "")

    def test_isn_credits_positive_passes(self) -> None:
        err = _validate({"tenant_schema": "ws_a",
                         "grant_type": "isn_credits", "credits": 1})
        self.assertEqual(err, "")
