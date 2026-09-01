"""
اختبارات نواة التوطين — تضمن أن فكّ القفل المصري يعمل بلا كسر:
عرض العملة الصحيح لكل دولة، اشتقاق الضريبة، والسقوط الآمن للافتراضي.
"""
from decimal import Decimal

from django.test import SimpleTestCase

from erp_core.localization import (
    format_money,
    currency_symbol,
    currency_for_country,
    vat_rate_for_country,
    country_config,
    resolve_tenant_localization,
    DEFAULT_COUNTRY,
)


class FormatMoneyTests(SimpleTestCase):
    def test_egp_uses_arabic_symbol_and_two_decimals(self):
        self.assertEqual(format_money(1234.5, 'EGP'), '1,234.50 ج.م')

    def test_aed_symbol(self):
        self.assertEqual(format_money(1000, 'AED'), '1,000.00 د.إ')

    def test_english_symbol_variant(self):
        self.assertEqual(format_money(50, 'USD', lang='en'), '50.00 $')

    def test_three_decimal_currency_kwd(self):
        self.assertEqual(format_money(2, 'KWD'), '2.000 د.ك')

    def test_none_amount_is_zero(self):
        self.assertEqual(format_money(None, 'AED'), '0.00 د.إ')

    def test_garbage_amount_is_zero_not_crash(self):
        self.assertEqual(format_money('not-a-number', 'EGP'), '0.00 ج.م')

    def test_symbol_can_be_suppressed(self):
        self.assertEqual(format_money(99.9, 'AED', symbol=False), '99.90')

    def test_decimals_override(self):
        self.assertEqual(format_money(1500000, 'EGP', decimals=0), '1,500,000 ج.م')

    def test_rounding_half_up(self):
        self.assertEqual(format_money(Decimal('1.005'), 'USD'), '1.01 $')

    def test_unknown_currency_falls_back_to_code(self):
        # عملة غير معرّفة → نعرض الكود نفسه بدل ما نكسر
        self.assertEqual(format_money(10, 'JPY'), '10.00 JPY')


class CountryConfigTests(SimpleTestCase):
    def test_uae_currency_and_vat(self):
        self.assertEqual(currency_for_country('AE'), 'AED')
        self.assertEqual(vat_rate_for_country('AE'), Decimal('5.00'))

    def test_egypt_defaults(self):
        cfg = country_config('EG')
        self.assertEqual(cfg['currency'], 'EGP')
        self.assertEqual(cfg['vat_rate'], Decimal('14.00'))
        self.assertEqual(cfg['timezone'], 'Africa/Cairo')

    def test_saudi_vat_fifteen(self):
        self.assertEqual(vat_rate_for_country('SA'), Decimal('15.00'))

    def test_unknown_country_falls_back_to_default(self):
        cfg = country_config('ZZ')
        self.assertEqual(cfg, country_config(DEFAULT_COUNTRY))

    def test_currency_symbol_lang(self):
        self.assertEqual(currency_symbol('SAR', 'ar'), 'ر.س')
        self.assertEqual(currency_symbol('SAR', 'en'), 'SAR')


class _FakeTenant:
    """stub بدون DB — resolve_tenant_localization يقرأ getattr فقط."""
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


class ResolveTenantLocalizationTests(SimpleTestCase):
    def test_country_drives_everything_when_fields_blank(self):
        loc = resolve_tenant_localization(_FakeTenant(country='AE'))
        self.assertEqual(loc['currency'], 'AED')
        self.assertEqual(loc['vat_rate'], Decimal('5.00'))
        self.assertEqual(loc['timezone'], 'Asia/Dubai')

    def test_explicit_fields_override_country(self):
        loc = resolve_tenant_localization(_FakeTenant(
            country='AE', currency='USD', vat_rate=Decimal('0.00'),
            timezone='America/New_York', default_language='en',
        ))
        self.assertEqual(loc['currency'], 'USD')
        self.assertEqual(loc['vat_rate'], Decimal('0.00'))
        self.assertEqual(loc['timezone'], 'America/New_York')
        self.assertEqual(loc['language'], 'en')

    def test_none_tenant_is_safe(self):
        loc = resolve_tenant_localization(None)
        self.assertEqual(loc['country'], DEFAULT_COUNTRY)
        self.assertEqual(loc['currency'], 'EGP')

    def test_partial_tenant_blank_currency_derives_from_country(self):
        loc = resolve_tenant_localization(_FakeTenant(country='SA', currency=''))
        self.assertEqual(loc['currency'], 'SAR')

    def test_zero_vat_rate_is_respected_not_treated_as_missing(self):
        # ضريبة صفر صريحة يجب ألا تُستبدل بضريبة الدولة
        loc = resolve_tenant_localization(_FakeTenant(country='SA', vat_rate=Decimal('0.00')))
        self.assertEqual(loc['vat_rate'], Decimal('0.00'))
