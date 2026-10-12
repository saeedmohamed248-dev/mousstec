"""
💸 Paying suppliers — part of an invoice, the whole of it, or on account.

* A payment on an invoice raises its «paid», lowers the vendor's balance and
  takes the cash from the treasury; it can't exceed what's left and a draft
  can't be paid.
* A payment on account settles the oldest open invoices first; anything
  beyond them is an advance.
* The vendor page flags a stored balance that no longer matches the invoices
  (the case where a 132,900 bill showed «الحساب مسوّى») and fixes it.
"""
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import connection
from django.test import Client, override_settings

from inventory.models import FinancialTransaction, PurchaseInvoice, Treasury, Vendor
from inventory.services.vendor_payments import (
    balance_mismatches, expected_balances, pay_invoice, pay_vendor,
)
from .base import ERPTenantTestCase
from .factories import make_branch, make_product, make_purchase_invoice, make_treasury, make_vendor
from .test_landed_cost import ledger_is_balanced

D = lambda v: Decimal(str(v))  # noqa: E731


@override_settings(ALLOWED_HOSTS=['*'])
class VendorPaymentTests(ERPTenantTestCase):
    def setUp(self):
        connection.set_tenant(self.tenant)
        self.host = self.domain.domain
        self.branch = make_branch()
        self.cash = make_treasury(self.branch, balance='100000')
        self.vendor = make_vendor(name='بيت الشمس')
        p = make_product(part_number='VP-1', name='موتور')
        self.old = self._post([(p, 1, '1000.00')])
        self.new = self._post([(p, 2, '1500.00')])
        self.boss = User.objects.create_user('vp_boss', password='x', is_staff=True, is_superuser=True)

    def _post(self, items):
        inv = make_purchase_invoice(self.vendor, self.branch, items=items)
        inv.status = 'posted'
        inv.save()
        inv.refresh_from_db()
        return inv

    def _state(self):
        self.vendor.refresh_from_db()
        self.cash.refresh_from_db()
        self.old.refresh_from_db()
        self.new.refresh_from_db()
        return self.vendor.balance, self.cash.balance, self.old.paid_amount, self.new.paid_amount

    def test_pay_part_then_the_rest_of_an_invoice(self):
        self.assertEqual(self._state(), (D('4000'), D('100000'), D('0'), D('0')))
        pay_invoice(self.new, '1000', self.cash, note='تحويل 55')
        self.assertEqual(self._state(), (D('3000'), D('99000'), D('0'), D('1000')))
        with self.assertRaisesMessage(ValidationError, 'أكبر من المتبقّي'):
            pay_invoice(self.new, '2500', self.cash)
        pay_invoice(self.new, '2000', self.cash)
        self.assertEqual(self._state(), (D('1000'), D('97000'), D('0'), D('3000')))
        self.assertEqual(self.new.net_due, D('0'))
        self.assertEqual(self.new.payments.count(), 2)
        self.assertEqual(expected_balances()[self.vendor.pk], D('1000'))
        self.assertTrue(ledger_is_balanced())

    def test_draft_and_empty_treasury_are_refused(self):
        draft = make_purchase_invoice(self.vendor, self.branch, items=[(make_product(part_number='VP-2'), 1, '50')])
        with self.assertRaisesMessage(ValidationError, 'مسودة'):
            pay_invoice(draft, '10', self.cash)
        with mock.patch('tenancy.signals.quota._current_tenant', return_value=None):   # حد الخزن في الباقة
            poor = make_treasury(self.branch, name='خزنة فاضية', balance='5')
        with self.assertRaisesMessage(ValidationError, 'مش كفاية'):
            pay_invoice(self.old, '10', poor)
        self.assertEqual(self._state()[0], D('4000'))

    def test_on_account_payment_settles_oldest_first_then_advance(self):
        allocated, advance = pay_vendor(self.vendor, '1500', self.cash)
        self.assertEqual([(i.pk, a) for i, a in allocated], [(self.old.pk, D('1000')), (self.new.pk, D('500'))])
        self.assertEqual(self._state(), (D('2500'), D('98500'), D('1000'), D('500')))
        allocated, advance = pay_vendor(self.vendor, '3000', self.cash)
        self.assertEqual(advance, D('500'))
        self.assertEqual(self._state(), (D('-500'), D('95500'), D('1000'), D('3000')))
        self.assertEqual(expected_balances()[self.vendor.pk], D('-500'))
        self.assertEqual(balance_mismatches(), [])
        self.assertTrue(ledger_is_balanced())

    def test_pages_pay_and_fix_a_drifted_balance(self):
        c = Client()
        c.force_login(self.boss)
        r = c.post(f'/system/purchases/{self.new.pk}/pay/',
                   {'treasury_id': self.cash.pk, 'full': '1', 'next': '/system/purchases/'}, HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 302)
        self.assertIn('ok=paid', r['Location'])
        self.assertEqual(self._state()[:2], (D('1000'), D('97000')))
        r = c.post(f'/system/purchases/{self.old.pk}/pay/',
                   {'treasury_id': self.cash.pk, 'amount': '5000', 'next': '/system/purchases/'}, HTTP_HOST=self.host)
        self.assertIn('err=', r['Location'])

        Vendor.objects.filter(pk=self.vendor.pk).update(balance=0)   # الرصيد اتلخبط
        page = c.get(f'/system/vendors/{self.vendor.pk}/', HTTP_HOST=self.host).content.decode()
        self.assertIn('الرصيد مش مطابق للفواتير', page)
        self.assertIn('openPay(', page)             # زرار سداد جنب الفاتورة المفتوحة
        self.assertIn('دفعة على الحساب', page)      # يظهر حتى والرصيد صفر
        listing = c.get('/system/vendors/', HTTP_HOST=self.host).content.decode()
        self.assertIn('مش مطابق', listing)
        c.post(f'/system/vendors/{self.vendor.pk}/fix-balance/', HTTP_HOST=self.host)
        self.assertEqual(self._state()[0], D('1000'))
        self.assertEqual(balance_mismatches(), [])

        r = c.post(f'/system/vendors/{self.vendor.pk}/pay/', {'treasury_id': self.cash.pk, 'amount': '1000'},
                   HTTP_HOST=self.host)
        self.assertIn('ok=paid', r['Location'])
        self.assertEqual(self._state(), (D('0'), D('96000'), D('1000'), D('3000')))
        self.assertEqual(FinancialTransaction.objects.filter(vendor=self.vendor, purchase_invoice__isnull=True).count(), 0)
        self.assertEqual(Treasury.objects.get(pk=self.cash.pk).balance, D('96000'))
        self.assertEqual(PurchaseInvoice.objects.get(pk=self.old.pk).net_due, D('0'))
