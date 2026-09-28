"""
📒 The general ledger follows the documents after they change.

* a treasury→treasury transfer is not income nor expense (clearing account);
* deleting a treasury movement removes its journal entry — or reverses it
  when its period is already closed;
* a posted sale invoice whose total changes afterwards (settlement discount,
  core return, edited line) gets an adjustment entry for the difference;
* ``backfill_ledger`` repairs transfers posted the old way.
"""
from datetime import date, timedelta
from decimal import Decimal
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.db import connection
from django.utils import timezone

from inventory.models import AccountingPeriod, FinancialTransaction, FiscalYear, JournalEntry
from inventory.services.accounting_service import TRANSFER_TAG, AccountingService
from .base import ERPTenantTestCase
from .factories import (
    make_branch, make_customer, make_inventory, make_product, make_sale_invoice, make_treasury,
)
from .test_full_accounting_cycle import acct_raw, ledger_is_balanced

D = lambda v: Decimal(str(v))  # noqa: E731


class LedgerSyncTests(ERPTenantTestCase):

    def setUp(self):
        super().setUp()
        connection.set_tenant(self.tenant)
        # Two treasuries (cash + bank) — above the test tenant's plan quota.
        patcher = mock.patch('tenancy.signals.quota._enforce', lambda **kw: None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.branch = make_branch()
        self.customer = make_customer()
        self.cash = make_treasury(self.branch, name='كاش', balance='1000.00')
        self.bank = make_treasury(self.branch, name='بنك', balance='0.00', type='bank')
        self.product = make_product(part_number='LS-1', retail_price='100.00', average_cost='60.00')
        make_inventory(self.product, self.branch, quantity=50)

    def _transfer(self, amount, src, dst, tag='abc12345'):
        ref = f"{TRANSFER_TAG}{tag}]"
        FinancialTransaction.objects.create(treasury=src, transaction_type='out', amount=D(amount),
                                            description=f"{ref} تحويل إلى {dst.name}")
        FinancialTransaction.objects.create(treasury=dst, transaction_type='in', amount=D(amount),
                                            description=f"{ref} تحويل من {src.name}")

    # ── transfers ─────────────────────────────────────────────────────
    def test_transfer_is_not_income_or_expense(self):
        self._transfer('300.00', self.cash, self.bank)
        self.assertEqual(acct_raw('4099'), D('0.00'))    # other revenue
        self.assertEqual(acct_raw('5099'), D('0.00'))    # general expense
        self.assertEqual(acct_raw('1090'), D('0.00'))    # clearing nets out
        self.assertEqual(acct_raw('1001'), D('-300.00'))
        self.assertEqual(acct_raw('1002'), D('300.00'))
        self.assertTrue(ledger_is_balanced())

    def test_backfill_reroutes_old_transfer_postings(self):
        self._transfer('120.00', self.cash, self.bank, tag='old00001')
        # Re-create the legacy (wrong) posting: expense + other revenue.
        for ft in FinancialTransaction.objects.filter(description__startswith=TRANSFER_TAG):
            JournalEntry.objects.filter(financial_transaction=ft).delete()
            other = 'general_expense' if ft.transaction_type == 'out' else 'other_revenue'
            cash = AccountingService._cash_key_for(ft.treasury)
            lines = ([{'account': other, 'debit': ft.amount, 'credit': 0},
                      {'account': cash, 'debit': 0, 'credit': ft.amount}]
                     if ft.transaction_type == 'out' else
                     [{'account': cash, 'debit': ft.amount, 'credit': 0},
                      {'account': other, 'debit': 0, 'credit': ft.amount}])
            AccountingService.post_journal(description='legacy', lines=lines, source=ft)
        self.assertEqual(acct_raw('4099'), D('-120.00'))

        call_command('backfill_ledger', schema=self.tenant.schema_name, stdout=StringIO())
        connection.set_tenant(self.tenant)
        self.assertEqual(acct_raw('4099'), D('0.00'))
        self.assertEqual(acct_raw('5099'), D('0.00'))
        self.assertEqual(acct_raw('1002'), D('120.00'))
        self.assertTrue(ledger_is_balanced())

    # ── deleting a treasury movement ─────────────────────────────────
    def test_deleting_movement_removes_its_entry(self):
        ft = FinancialTransaction.objects.create(treasury=self.cash, transaction_type='out',
                                                 amount=D('50.00'), description='كهربا')
        self.assertEqual(acct_raw('5099'), D('50.00'))
        FinancialTransaction.objects.filter(pk=ft.pk).delete()   # admin bulk delete
        self.assertEqual(acct_raw('5099'), D('0.00'))
        self.assertEqual(acct_raw('1001'), D('0.00'))
        self.assertFalse(JournalEntry.objects.filter(financial_transaction__isnull=True,
                                                     journal_type='cash_payment').exists())
        self.cash.refresh_from_db()
        self.assertEqual(self.cash.balance, D('1000.00'))

    def test_deleting_movement_in_closed_period_reverses_instead(self):
        last_month = timezone.now() - timedelta(days=40)
        fy = FiscalYear.objects.create(code='FY-LS', name='سنة', start_date=date(2000, 1, 1),
                                       end_date=date(2999, 12, 31))
        period = AccountingPeriod.objects.create(
            fiscal_year=fy, name='فترة قديمة', start_date=last_month.date() - timedelta(days=5),
            end_date=last_month.date() + timedelta(days=5))
        ft = FinancialTransaction.objects.create(treasury=self.cash, transaction_type='out',
                                                 amount=D('80.00'), description='إيجار', date=last_month)
        original = JournalEntry.objects.get(financial_transaction=ft)
        AccountingPeriod.objects.filter(pk=period.pk).update(is_closed=True)

        ft.delete()
        original.refresh_from_db()
        self.assertEqual(original.status, 'reversed')          # closed books untouched
        self.assertTrue(JournalEntry.objects.filter(reversal_of=original,
                                                    date=timezone.localdate()).exists())
        self.assertEqual(acct_raw('5099'), D('0.00'))
        self.assertTrue(ledger_is_balanced())

    # ── posted invoice changes afterwards ────────────────────────────
    def _credit_sale(self, qty=2, price='100.00'):
        si = make_sale_invoice(customer=self.customer, branch=self.branch,
                               items=[(self.product, qty, price)])
        si.status = 'posted'
        si.save()
        si.refresh_from_db()
        return si

    def test_settlement_discount_reaches_the_ledger(self):
        si = self._credit_sale()
        self.assertEqual(acct_raw('1100'), D('200.00'))   # AR
        self.assertEqual(acct_raw('4001'), D('-200.00'))  # revenue

        si.discount = D('50.00')          # «خصم على المتبقّي» from the invoices list
        si.update_total()
        self.assertEqual(acct_raw('1100'), D('150.00'))
        self.assertEqual(acct_raw('4001'), D('-150.00'))
        self.assertTrue(JournalEntry.objects.filter(sale_invoice=si, journal_type='adjustment').exists())
        self.assertIsNone(AccountingService.sync_sale_invoice(si))   # idempotent
        self.assertTrue(ledger_is_balanced())

    def test_unposting_a_sale_in_an_open_period_deletes_it(self):
        si = self._credit_sale()
        fy = FiscalYear.objects.create(code='FY-LS2', name='سنة', start_date=date(2000, 1, 1),
                                       end_date=date(2999, 12, 31))
        AccountingPeriod.objects.create(fiscal_year=fy, name='مقفولة', is_closed=True,
                                        start_date=date(2000, 1, 1),
                                        end_date=timezone.localdate() - timedelta(days=400))
        # The sale's own period (none recorded) is open → plain delete.
        deleted, reversed_ = AccountingService.unpost(JournalEntry.objects.filter(sale_invoice=si))
        self.assertEqual((deleted, reversed_), (1, 0))
        self.assertEqual(acct_raw('1100'), D('0.00'))
        self.assertTrue(ledger_is_balanced())

    # ── core-charge (توالف) refund ───────────────────────────────────
    def _core_sale(self, paid):
        core = make_product(part_number='CORE-1', retail_price='100.00', average_cost='60.00',
                            core_charge=D('30.00'))
        make_inventory(core, self.branch, quantity=5)
        si = make_sale_invoice(customer=self.customer, branch=self.branch, treasury=self.cash,
                               items=[(core, 1, '100.00')], paid_amount=paid)
        si.status = 'posted'
        si.save()
        si.refresh_from_db()
        self.assertEqual(si.total_amount, D('130.00'))
        return si, si.items.get()

    def test_core_refund_on_credit_sale_only_lowers_the_debt(self):
        si, item = self._core_sale(paid='0.00')
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.balance, D('130.00'))
        item.is_core_returned = True
        item.save()
        self.customer.refresh_from_db()
        si.refresh_from_db()
        self.assertEqual(self.customer.balance, D('100.00'))
        self.assertEqual(si.total_amount, D('100.00'))
        self.assertFalse(FinancialTransaction.objects.filter(transaction_type='out').exists())
        self.assertEqual(acct_raw('1100'), D('100.00'))
        self.assertTrue(ledger_is_balanced())

    def test_core_refund_on_paid_sale_pays_cash_once(self):
        si, item = self._core_sale(paid='130.00')
        item.is_core_returned = True
        item.save()
        self.customer.refresh_from_db()
        si.refresh_from_db()
        self.cash.refresh_from_db()
        self.assertEqual(self.customer.balance, D('0.00'))      # was −30 (refunded twice)
        self.assertEqual((si.total_amount, si.paid_amount), (D('100.00'), D('100.00')))
        self.assertEqual(self.cash.balance, D('1100.00'))       # +130 − 30
        self.assertEqual(acct_raw('1100'), D('0.00'))
        self.assertEqual(acct_raw('4001'), D('-100.00'))
        self.assertTrue(ledger_is_balanced())


class PurchaseReturnTests(ERPTenantTestCase):
    """↩️ Returning part of a received purchase to the vendor."""

    def setUp(self):
        super().setUp()
        connection.set_tenant(self.tenant)
        from .factories import make_vendor
        self.branch = make_branch()
        self.vendor = make_vendor()
        self.cash = make_treasury(self.branch, name='كاش', balance='5000.00')
        self.product = make_product(part_number='PR-1', average_cost='50.00', purchase_price='50.00')
        make_inventory(self.product, self.branch, quantity=0)

    def _receive(self, paid):
        from .factories import make_purchase_invoice
        pi = make_purchase_invoice(self.vendor, self.branch, treasury=self.cash,
                                   items=[(self.product, 10, '50.00')], paid_amount=paid)
        pi.status = 'posted'
        pi.save()
        pi.refresh_from_db()
        self.assertTrue(pi.is_applied)
        return pi

    def _stock(self):
        from inventory.models import Inventory
        return Inventory.objects.get(product=self.product, branch=self.branch).quantity

    def test_credit_purchase_return_lowers_payable_and_stock(self):
        from inventory.services.purchase_return_service import return_to_vendor
        pi = self._receive(paid='0.00')
        self.vendor.refresh_from_db()
        self.assertEqual(self.vendor.balance, D('500.00'))
        item = pi.items.get()

        res = return_to_vendor(pi, [(item.pk, 4)], note='معيبة')
        self.assertEqual(res['vendor_value'], D('200.00'))
        pi.refresh_from_db()
        item.refresh_from_db()
        self.vendor.refresh_from_db()
        self.assertEqual(self._stock(), 6)
        self.assertEqual(item.returned_quantity, 4)
        self.assertEqual((pi.returned_amount, pi.net_due), (D('200.00'), D('300.00')))
        self.assertEqual(self.vendor.balance, D('300.00'))
        self.assertEqual(acct_raw('2100'), D('-300.00'))   # AP
        self.assertEqual(acct_raw('1200'), D('300.00'))    # inventory
        self.assertTrue(ledger_is_balanced())

        # Can't send back more than is left, and the whole-invoice reversal is now blocked.
        from django.core.exceptions import ValidationError
        with self.assertRaises(ValidationError):
            return_to_vendor(pi, [(item.pk, 7)])
        from inventory.views_lightning import _reverse_purchase_posting
        with self.assertRaises(ValueError):
            _reverse_purchase_posting(pi)

    def test_paid_purchase_return_with_cash_refund(self):
        from django.core.exceptions import ValidationError
        from inventory.services.purchase_return_service import return_to_vendor
        pi = self._receive(paid='500.00')
        item = pi.items.get()
        with self.assertRaises(ValidationError):           # refund must land in a treasury
            return_to_vendor(pi, [(item.pk, 2)], refund_amount='100.00')
        with self.assertRaises(ValidationError):           # more than the returned value
            return_to_vendor(pi, [(item.pk, 2)], refund_amount='150.00', refund_treasury=self.cash)

        return_to_vendor(pi, [(item.pk, 2)], refund_amount='100.00', refund_treasury=self.cash)
        pi.refresh_from_db()
        self.vendor.refresh_from_db()
        self.cash.refresh_from_db()
        self.assertEqual(self.cash.balance, D('4600.00'))   # 5000 − 500 paid + 100 back
        self.assertEqual((pi.paid_amount, pi.returned_amount, pi.net_due),
                         (D('400.00'), D('100.00'), D('0.00')))
        self.assertEqual(self.vendor.balance, D('0.00'))
        self.assertEqual(acct_raw('2100'), D('0.00'))
        self.assertEqual(self._stock(), 8)
        self.assertTrue(ledger_is_balanced())
