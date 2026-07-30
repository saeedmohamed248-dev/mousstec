"""Currency display-layer conversion — math + formatting.

These tests mock ``get_rate`` so they exercise the conversion/formatting/
rounding logic without touching the DB or a tenant schema. The ledger is
never involved — this is the display layer only.
"""
from __future__ import annotations

from decimal import Decimal
from unittest import mock

from django.test import SimpleTestCase

from clients.services import currency


class CurrencyConversionTests(SimpleTestCase):
    def test_base_currency_is_identity(self):
        self.assertEqual(currency.get_rate('EGP'), Decimal('1'))
        # convert of EGP→EGP returns the same amount (2 decimals)
        self.assertEqual(currency.convert(Decimal('100'), 'EGP'), Decimal('100.00'))

    def test_unsupported_currency(self):
        self.assertFalse(currency.is_supported('XYZ'))
        self.assertIsNone(currency.get_rate('XYZ'))
        self.assertIsNone(currency.convert(Decimal('100'), 'XYZ'))

    @mock.patch('clients.services.currency.get_rate', return_value=Decimal('0.02'))
    def test_convert_applies_rate_and_rounds(self, _rate):
        # 1000 EGP × 0.02 = 20.00 USD
        self.assertEqual(currency.convert(Decimal('1000'), 'USD'), Decimal('20.00'))

    @mock.patch('clients.services.currency.get_rate', return_value=Decimal('0.076123'))
    def test_kwd_uses_three_decimals(self, _rate):
        # KWD is a 3-decimal currency
        self.assertEqual(currency.convert(Decimal('100'), 'KWD'), Decimal('7.612'))

    @mock.patch('clients.services.currency.get_rate', return_value=Decimal('0.02'))
    def test_format_amount_includes_symbol(self, _rate):
        out = currency.format_amount(Decimal('1000'), 'USD')
        self.assertIn('$', out)
        self.assertIn('20.00', out)

    @mock.patch('clients.services.currency.get_rate', return_value=None)
    def test_format_amount_falls_back_to_egp_when_no_rate(self, _rate):
        # No USD rate available → show the original EGP amount, never crash
        out = currency.format_amount(Decimal('1000'), 'USD')
        self.assertIn('ج.م', out)
        self.assertIn('1000', out)
