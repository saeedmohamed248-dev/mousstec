"""HR pure-calculation coverage — leave duration + advance installments.

These read computed properties on unsaved model instances, so they need no
DB. They pin the arithmetic payroll and leave approval depend on.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from django.test import SimpleTestCase

from hr.models import Advance, LeaveRequest


class LeaveDurationTests(SimpleTestCase):
    def test_single_day_leave_is_one_day(self):
        lr = LeaveRequest(from_date=date(2026, 7, 30), to_date=date(2026, 7, 30))
        self.assertEqual(lr.total_days, 1)

    def test_multi_day_leave_is_inclusive(self):
        lr = LeaveRequest(from_date=date(2026, 7, 1), to_date=date(2026, 7, 5))
        self.assertEqual(lr.total_days, 5)  # inclusive of both endpoints

    def test_missing_dates_returns_zero(self):
        self.assertEqual(LeaveRequest().total_days, 0)


class AdvanceInstallmentTests(SimpleTestCase):
    def test_even_split(self):
        adv = Advance(amount=Decimal('1200'), installments_count=3)
        self.assertEqual(adv.installment_amount, Decimal('400.00'))

    def test_rounds_to_two_places(self):
        adv = Advance(amount=Decimal('1000'), installments_count=3)
        self.assertEqual(adv.installment_amount, Decimal('333.33'))

    def test_zero_installments_returns_full_amount(self):
        adv = Advance(amount=Decimal('500'), installments_count=0)
        self.assertEqual(adv.installment_amount, Decimal('500'))
