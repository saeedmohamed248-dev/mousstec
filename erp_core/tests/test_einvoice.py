"""اختبارات الفاتورة الضريبية: تفكيك VAT + TLV QR + سياق القالب."""
from decimal import Decimal

from django.test import SimpleTestCase

from erp_core.einvoice import vat_breakdown, build_tlv_base64, build_tax_invoice_context


class VatBreakdownTests(SimpleTestCase):
    def test_five_percent_uae(self):
        # إجمالي 105 شامل 5% ⇒ صافي 100 + ضريبة 5
        bd = vat_breakdown(Decimal('105.00'), Decimal('5'))
        self.assertEqual(bd['net'], Decimal('100.00'))
        self.assertEqual(bd['vat'], Decimal('5.00'))

    def test_zero_rate(self):
        bd = vat_breakdown(Decimal('200.00'), Decimal('0'))
        self.assertEqual(bd['net'], Decimal('200.00'))
        self.assertEqual(bd['vat'], Decimal('0.00'))

    def test_net_plus_vat_equals_total(self):
        bd = vat_breakdown(Decimal('470.40'), Decimal('5'))
        self.assertEqual(bd['net'] + bd['vat'], Decimal('470.40'))


class TlvQrTests(SimpleTestCase):
    def test_tlv_is_base64_and_decodes(self):
        import base64
        b64 = build_tlv_base64('Al Noor Auto', '100399492500003', '2026-08-30T12:00:00', 105, 5)
        raw = base64.b64decode(b64)
        # أول عنصر: tag=1، ثم الطول، ثم اسم البائع
        self.assertEqual(raw[0], 1)
        self.assertEqual(raw[1], len('Al Noor Auto'.encode()))
        self.assertIn(b'100399492500003', raw)  # TRN موجود


class _FakeTenant:
    def __init__(self, name='', trn=''):
        self.name = name
        self.tax_registration_number = trn


class _FakeInvoice:
    def __init__(self, total, rate):
        self.total_amount = total
        self.tax_percentage = rate
        self.date_created = None


class TaxInvoiceContextTests(SimpleTestCase):
    def test_registered_seller_gets_tax_invoice(self):
        ctx = build_tax_invoice_context(_FakeInvoice(Decimal('105'), Decimal('5')),
                                        _FakeTenant('Shop', '100399492500003'))
        self.assertTrue(ctx['is_tax_invoice'])
        self.assertEqual(ctx['seller_trn'], '100399492500003')
        self.assertEqual(ctx['vat_amount'], Decimal('5.00'))

    def test_unregistered_seller_no_tax_invoice(self):
        ctx = build_tax_invoice_context(_FakeInvoice(Decimal('105'), Decimal('5')),
                                        _FakeTenant('Shop', ''))
        self.assertFalse(ctx['is_tax_invoice'])
        self.assertEqual(ctx['tax_qr_data_uri'], '')

    def test_none_tenant_is_safe(self):
        ctx = build_tax_invoice_context(_FakeInvoice(Decimal('100'), Decimal('0')), None)
        self.assertFalse(ctx['is_tax_invoice'])
