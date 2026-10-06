"""
🧪 Money bugs found by running the spare-parts system end to end.

Each test reproduces what a shop would actually do and checks the stock,
customer/vendor/employee balances and the general ledger afterwards:

* expense category #1 (salaries) was posted to account 5001 = COGS;
* lines the accountant marked "not billable" were still charged;
* a sale return ignored the invoice's VAT and invoice-level discount and
  restored stock at today's average cost instead of the cost sold at;
* deleting a never-posted quotation added phantom stock and a phantom
  customer credit;
* two draft purchase orders for the same vendor crashed every sale that
  hit the low-stock reorder;
* the income statement of a closed period showed zero profit;
* salespeople earned commission on quotations nobody bought, and paying a
  commission was booked as a second expense;
* a purchase edit that failed half-way committed its half-done reversal;
* the P&L report counted VAT as profit and owner drawings as expenses.
"""
import json
from datetime import date
from decimal import Decimal

from django.contrib.auth.models import User
from django.db import connection
from django.db.models import Sum
from django.test import Client, override_settings

from inventory.models import (
    AccountingEntry, AccountingPeriod, ChartOfAccount, Customer, EmployeeProfile,
    ExpenseCategory, FiscalYear, Inventory, PurchaseInvoice, SaleInvoice, SaleInvoiceItem,
    SaleInvoiceServiceItem, ServiceCatalog,
)
from inventory.services.accounting_reports import AccountingReportService
from inventory.services.accounting_service import AccountingService
from inventory.services.invoice_service import InvoiceService
from .base import ERPTenantTestCase
from .factories import (
    make_branch, make_customer, make_employee, make_financial_transaction, make_inventory,
    make_product, make_sale_invoice, make_treasury, make_vendor,
)

D = lambda v: Decimal(str(v))  # noqa: E731


def acct(code):
    """Signed (debit − credit) balance of an account."""
    a = ChartOfAccount.objects.filter(code=code).first()
    if a is None:
        return D('0.00')
    agg = AccountingEntry.objects.filter(account=a).aggregate(d=Sum('debit'), c=Sum('credit'))
    return (agg['d'] or D(0)) - (agg['c'] or D(0))


def ledger_is_balanced():
    agg = AccountingEntry.objects.aggregate(d=Sum('debit'), c=Sum('credit'))
    return (agg['d'] or D(0)) == (agg['c'] or D(0))


@override_settings(ALLOWED_HOSTS=['*'])
class ReviewMoneyFixesTests(ERPTenantTestCase):

    def setUp(self):
        super().setUp()
        connection.set_tenant(self.tenant)
        self.host = self.domain.domain
        self.branch = make_branch()
        self.cash = make_treasury(self.branch, balance='5000.00')
        self.customer = make_customer()
        self.vendor = make_vendor()
        self.product = make_product(part_number='RV-1', retail_price='100.00', average_cost='60.00')
        make_inventory(self.product, self.branch, quantity=50)
        self.owner = User.objects.create_user('owner_rv', password='x', is_staff=True, is_superuser=True)

    def _client(self):
        c = Client()
        c.force_login(self.owner)
        return c

    def _stock(self):
        return Inventory.objects.get(product=self.product, branch=self.branch).quantity

    def _post(self, invoice):
        invoice.status = 'posted'
        invoice.save()
        invoice.refresh_from_db()
        return invoice

    # ── expenses ──────────────────────────────────────────────────────
    @staticmethod
    def _salaries():
        # The migrations seed «رواتب وأجور» first → pk 1 (old code «5001» = COGS).
        cat, _ = ExpenseCategory.objects.update_or_create(pk=1, defaults={'name': 'رواتب وأجور'})
        return cat

    def test_salary_category_is_not_posted_to_cost_of_goods_sold(self):
        salaries = self._salaries()
        make_financial_transaction(self.cash, '1000', 'out', category=salaries, description='رواتب')
        self.assertEqual(acct('5001'), D('0.00'))
        self.assertEqual(acct(AccountingService.category_account_code(salaries)), D('1000.00'))
        self.assertTrue(ledger_is_balanced())

    def test_backfill_moves_old_category_postings_off_system_accounts(self):
        from io import StringIO
        from django.core.management import call_command
        salaries = self._salaries()
        ft = make_financial_transaction(self.cash, '700', 'out', category=salaries, description='رواتب')
        # Simulate a line posted by the old code scheme (5 + id → 5001 = COGS).
        cogs = AccountingService.account('cogs')
        AccountingEntry.objects.filter(journal_entry__financial_transaction=ft, debit__gt=0).update(account=cogs)
        self.assertEqual(acct('5001'), D('700.00'))
        call_command('backfill_ledger', schema=self.tenant.schema_name, stdout=StringIO())
        self.assertEqual(acct('5001'), D('0.00'))
        self.assertEqual(acct(AccountingService.category_account_code(salaries)), D('700.00'))

    # ── accountant review (billable) ─────────────────────────────────
    def test_non_billable_lines_are_not_charged(self):
        si = make_sale_invoice(self.customer, self.branch, items=[(self.product, 1, '100.00')])
        svc = ServiceCatalog.objects.create(name='فحص', labor_price=D('300'), estimated_hours=D('1'))
        SaleInvoiceServiceItem.objects.create(invoice=si, service=svc, is_billable=False)
        si.refresh_from_db()
        self.assertEqual(si.total_amount, D('100.00'))

    def test_review_unbilling_a_work_order_line_moves_balance_and_ledger(self):
        svc = ServiceCatalog.objects.create(name='فحص كمبيوتر', labor_price=D('300'), estimated_hours=D('1'))
        r = self._client().post('/system/job-card/save/', data=json.dumps({
            'branch_id': self.branch.pk, 'customer_id': self.customer.pk,
            'items': [{'product_id': self.product.pk, 'qty': 1, 'price': 100}],
            'services': [{'service_id': svc.pk}],
        }), content_type='application/json', HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 200, r.content)
        si = SaleInvoice.objects.get(pk=json.loads(r.content)['invoice_id'])
        self.assertEqual(si.total_amount, D('400.00'))
        item = si.items.get()
        # Accountant keeps the part, drops the 300 diagnostic.
        r = self._client().post(f'/system/invoice/{si.pk}/review/',
                                {f'item_{item.pk}_billable': 'on'}, HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 302)
        si.refresh_from_db()
        self.customer.refresh_from_db()
        self.assertEqual(si.total_amount, D('100.00'))
        self.assertEqual(self.customer.balance, D('100.00'))
        self.assertEqual(acct('1100'), D('100.00'))
        self.assertTrue(ledger_is_balanced())

    # ── returns ───────────────────────────────────────────────────────
    def test_return_carries_vat_and_invoice_discount(self):
        si = make_sale_invoice(self.customer, self.branch, treasury=self.cash,
                               items=[(self.product, 2, '100.00')])
        si.tax_percentage = D('14')
        si.discount = D('20')
        si.save()
        si.update_total()
        si.refresh_from_db()
        si.paid_amount = si.total_amount             # (200 − 20) × 1.14
        self._post(si)
        self.assertEqual(si.total_amount, D('205.20'))
        ret = InvoiceService.create_return_invoice(si)
        self.assertEqual(ret.total_amount, D('205.20'))
        self.assertEqual(ret.paid_amount, D('205.20'))
        self._post(ret)
        self.assertEqual(acct('2200'), D('0.00'))    # VAT on returned goods reversed
        self.assertEqual(acct('1100'), D('0.00'))
        self.assertEqual(acct('4001') + acct('4090'), D('0.00'))   # returns = revenue, no more
        self.assertTrue(ledger_is_balanced())

    def test_return_restores_stock_at_the_cost_it_was_sold_at(self):
        si = self._post(make_sale_invoice(self.customer, self.branch, treasury=self.cash,
                                          items=[(self.product, 1, '100.00')]))
        self.product.average_cost = D('90.00')
        self.product.save()
        ret = InvoiceService.create_return_invoice(si)
        self.assertEqual(ret.items.get().cost_at_sale, D('60.00'))
        self.assertEqual(ret.total_cost, D('60.00'))

    # ── delete ────────────────────────────────────────────────────────
    def test_deleting_a_quotation_leaves_stock_and_balance_alone(self):
        si = make_sale_invoice(self.customer, self.branch, items=[(self.product, 3, '100.00')])
        r = self._client().post(f'/system/invoices/{si.pk}/delete/', HTTP_HOST=self.host)
        self.assertIn('deleted=', r['Location'])
        self.assertEqual(self._stock(), 50)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.balance, D('0.00'))

    def test_collecting_on_a_quotation_is_refused(self):
        si = make_sale_invoice(self.customer, self.branch, items=[(self.product, 1, '100.00')])
        r = self._client().post(f'/system/invoices/{si.pk}/pay/', data=json.dumps(
            {'treasury_id': self.cash.pk, 'amount': 100}),
            content_type='application/json', HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 400)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.balance, D('0.00'))

    # ── auto-reorder ──────────────────────────────────────────────────
    def test_two_draft_purchase_orders_do_not_break_a_sale(self):
        self.product.min_stock_level = 49
        self.product.save()
        PurchaseInvoice.objects.create(vendor=self.vendor, branch=self.branch, status='draft')
        PurchaseInvoice.objects.create(vendor=self.vendor, branch=self.branch, status='draft')
        si = self._post(make_sale_invoice(self.customer, self.branch, treasury=self.cash,
                                          items=[(self.product, 2, '100.00')]))
        self.assertTrue(si.is_applied)
        self.assertEqual(self._stock(), 48)

    # ── period close ──────────────────────────────────────────────────
    def test_closed_period_income_statement_keeps_its_profit(self):
        today = date.today()
        fy = FiscalYear.objects.create(code='FYR', name='FYR', start_date=today.replace(month=1, day=1),
                                       end_date=today.replace(month=12, day=31))
        period = AccountingPeriod.objects.create(fiscal_year=fy, name='P', start_date=today.replace(day=1),
                                                 end_date=today)
        self._post(make_sale_invoice(self.customer, self.branch, treasury=self.cash,
                                     items=[(self.product, 1, '100.00')]))
        before = AccountingReportService.income_statement(period.start_date, period.end_date)['net_profit']
        AccountingService.close_period(period)
        after = AccountingReportService.income_statement(period.start_date, period.end_date)['net_profit']
        self.assertEqual(before, D('40.00'))
        self.assertEqual(after, D('40.00'))
        self.assertTrue(AccountingReportService.balance_sheet()['is_balanced'])

    # ── commissions ───────────────────────────────────────────────────
    def test_technician_commission_payout_settles_the_liability(self):
        from inventory.services.treasury_service import TreasuryService
        _u, tech = make_employee(username='tech_rv', role='tech')
        svc = ServiceCatalog.objects.create(name='تغيير زيت', labor_price=D('200'), estimated_hours=D('1'),
                                            tech_commission_percent=D('10'))
        si = make_sale_invoice(self.customer, self.branch, items=[(self.product, 1, '100.00')])
        SaleInvoiceServiceItem.objects.create(invoice=si, service=svc, technician=tech, actual_hours=D('1'))
        self._post(si)
        tech.refresh_from_db()
        self.assertEqual(tech.commission_balance, D('20.00'))
        self.assertEqual(acct('2110'), D('-20.00'))
        self.assertEqual(acct('5210'), D('20.00'))
        TreasuryService.pay_commissions(EmployeeProfile.objects.filter(pk=tech.pk), self.cash)
        self.assertEqual(acct('2110'), D('0.00'))
        self.assertEqual(acct('5210'), D('20.00'))     # expensed once, not twice
        self.assertEqual(acct('5099'), D('0.00'))
        self.assertTrue(ledger_is_balanced())

    def test_deleting_a_posted_sale_takes_the_technician_commission_back(self):
        _u, tech = make_employee(username='tech_rv2', role='tech')
        svc = ServiceCatalog.objects.create(name='فرامل', labor_price=D('200'), estimated_hours=D('1'),
                                            tech_commission_percent=D('10'))
        si = make_sale_invoice(self.customer, self.branch, items=[(self.product, 1, '100.00')])
        SaleInvoiceServiceItem.objects.create(invoice=si, service=svc, technician=tech, actual_hours=D('1'))
        self._post(si)
        self._client().post(f'/system/invoices/{si.pk}/delete/', HTTP_HOST=self.host)
        tech.refresh_from_db()
        self.assertEqual(tech.commission_balance, D('0.00'))
        self.assertEqual(acct('2110'), D('0.00'))
        self.assertEqual(self._stock(), 50)

    def test_work_order_delivery_pays_the_assigned_technician(self):
        _u, tech = make_employee(username='tech_rv3', role='tech')
        svc = ServiceCatalog.objects.create(name='ضبط زوايا', labor_price=D('150'), estimated_hours=D('1'),
                                            tech_commission_percent=D('10'))
        r = self._client().post('/system/job-card/save/', data=json.dumps({
            'branch_id': self.branch.pk, 'customer_id': self.customer.pk,
            'services': [{'service_id': svc.pk}],
        }), content_type='application/json', HTTP_HOST=self.host)
        si = SaleInvoice.objects.get(pk=json.loads(r.content)['invoice_id'])
        si.service_items.update(technician=tech)        # assigned on the shop floor
        self._post(si)                                    # delivered
        tech.refresh_from_db()
        self.assertEqual(tech.commission_balance, D('15.00'))
        self._post(si)                                    # re-save: no double pay
        tech.refresh_from_db()
        self.assertEqual(tech.commission_balance, D('15.00'))

    def test_deleting_a_commission_payout_gives_the_commission_back(self):
        from inventory.models import FinancialTransaction
        from inventory.services.treasury_service import TreasuryService
        _u, sp = make_employee(username='sp_rv', role='sales', commission_balance='80.00')
        TreasuryService.pay_commissions(EmployeeProfile.objects.filter(pk=sp.pk), self.cash)
        sp.refresh_from_db()
        self.assertEqual(sp.commission_balance, D('0.00'))
        FinancialTransaction.objects.get(employee=sp).delete()
        sp.refresh_from_db()
        self.cash.refresh_from_db()
        self.assertEqual(sp.commission_balance, D('80.00'))
        self.assertEqual(self.cash.balance, D('5000.00'))

    # ── purchases ─────────────────────────────────────────────────────
    def test_failed_purchase_edit_rolls_back_completely(self):
        c = self._client()
        r = c.post('/system/purchases/save/', data=json.dumps({
            'branch_id': self.branch.pk, 'vendor_id': self.vendor.pk,
            'items': [{'product_id': self.product.pk, 'qty': 5, 'cost': 50}],
        }), content_type='application/json', HTTP_HOST=self.host)
        pid = json.loads(r.content)['invoice_id']
        self.assertEqual(self._stock(), 55)
        r = c.post('/system/purchases/save/', data=json.dumps({
            'invoice_id': pid, 'branch_id': self.branch.pk, 'vendor_id': self.vendor.pk,
            'treasury_id': self.cash.pk, 'paid_amount': 999999,
            'items': [{'product_id': self.product.pk, 'qty': 5, 'cost': 5000}],
        }), content_type='application/json', HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 409)
        pi = PurchaseInvoice.objects.get(pk=pid)
        self.vendor.refresh_from_db()
        self.assertEqual((pi.status, pi.is_applied, pi.total_amount), ('posted', True, D('250.00')))
        self.assertEqual(self._stock(), 55)
        self.assertEqual(self.vendor.balance, D('250.00'))
        self.assertEqual(acct('2100'), D('-250.00'))

    # ── reports ───────────────────────────────────────────────────────
    def test_pnl_report_excludes_vat_and_owner_drawings(self):
        si = make_sale_invoice(self.customer, self.branch, treasury=self.cash,
                               items=[(self.product, 1, '100.00')])
        si.tax_percentage = D('14')
        si.save()
        si.update_total()
        si.refresh_from_db()
        si.paid_amount = si.total_amount
        self._post(si)
        make_financial_transaction(self.cash, '50', 'out', equity_kind='drawings', description='سحب')
        r = self._client().get('/system/reports/pnl/?period=month', HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context['net_sales'], D('100.00'))
        self.assertEqual(r.context['total_exp'], D('0'))
        self.assertEqual(r.context['net_profit'], D('40.00'))

    def test_cashier_cannot_beat_the_discount_limit_by_lowering_the_price(self):
        cashier, profile = make_employee(username='cashier_rv', role='cashier', branch=self.branch)
        profile.max_discount_pct = D('5')
        profile.save()
        c = Client()
        c.force_login(cashier)
        r = c.post('/system/lightning-pos/checkout/', data=json.dumps({
            'branch_id': self.branch.pk,
            'payments': [{'treasury_id': self.cash.pk, 'amount': 50}],
            'items': [{'product_id': self.product.pk, 'qty': 1, 'price': 50, 'discount': 0}],
        }), content_type='application/json', HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 403, r.content)
        self.assertEqual(self._stock(), 50)

    # ── fast POS screen (offline-sync endpoint) ──────────────────────
    def test_fast_pos_sale_takes_stock_and_puts_cash_in_the_treasury(self):
        c = self._client()
        r = c.post('/system/api/v1/inventory/offline-sync/', data=json.dumps({'invoices': [
            {'local_id': 'L-1', 'items': [{'product_id': self.product.pk, 'quantity': 30,
                                           'unit_price': 100}]},
        ]}), content_type='application/json', HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 200, r.content)
        body = json.loads(r.content)
        self.assertEqual((body['synced'], body['failed']), (1, []))
        self.assertEqual(self._stock(), 20)        # 30 of 50 — used to fail the whole batch
        self.cash.refresh_from_db()
        self.assertEqual(self.cash.balance, D('8000.00'))
        self.assertEqual(acct('1100'), D('0.00'))
        self.assertEqual(acct('4001'), D('-3000.00'))
        self.assertEqual(acct('5001'), D('1800.00'))
        # Re-sending the same queue is a no-op.
        r = c.post('/system/api/v1/inventory/offline-sync/', data=json.dumps({'invoices': [
            {'local_id': 'L-1', 'items': [{'product_id': self.product.pk, 'quantity': 30,
                                           'unit_price': 100}]},
        ]}), content_type='application/json', HTTP_HOST=self.host)
        self.assertEqual(json.loads(r.content)['skipped'], 1)
        self.assertEqual(self._stock(), 20)

    def test_fast_pos_sale_without_stock_is_reported_not_dropped(self):
        r = self._client().post('/system/api/v1/inventory/offline-sync/', data=json.dumps({'invoices': [
            {'local_id': 'L-2', 'items': [{'product_id': self.product.pk, 'quantity': 99,
                                           'unit_price': 100}]},
        ]}), content_type='application/json', HTTP_HOST=self.host)
        body = json.loads(r.content)
        self.assertEqual(body['synced'], 0)
        self.assertEqual([f['local_id'] for f in body['failed']], ['L-2'])
        self.assertEqual(self._stock(), 50)
        self.assertFalse(SaleInvoice.objects.filter(notes__contains='[OFFLINE:L-2]').exists())

    # ── stock count ───────────────────────────────────────────────────
    def test_stock_count_shortage_does_not_take_cash_from_the_drawer(self):
        r = self._client().post('/system/api/v1/inventory/cycle-count/', data=json.dumps({
            'barcode': self.product.part_number, 'actual_qty': 47, 'branch_id': self.branch.pk,
        }), content_type='application/json', HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 200, r.content)
        self.cash.refresh_from_db()
        self.assertEqual(self.cash.balance, D('5000.00'))
        self.assertEqual(self._stock(), 47)
        self.assertEqual(acct('1200'), D('-180.00'))   # 3 × 60 out of inventory
        self.assertEqual(acct('5160'), D('180.00'))    # shrinkage expense
        self.assertEqual(acct('1001'), D('0.00'))

    # ── dates ─────────────────────────────────────────────────────────
    def test_journal_entry_is_dated_in_shop_time_not_utc(self):
        import datetime as _dt
        from unittest import mock
        from django.utils import timezone
        # 01:30 in Cairo on the 1st = 22:30 UTC on the last day of the month before.
        cairo_now = timezone.make_aware(_dt.datetime(2026, 10, 1, 1, 30))
        with mock.patch('django.utils.timezone.now', return_value=cairo_now):
            je = AccountingService.post_journal(
                description='اختبار', lines=[
                    {'account': 'cash', 'debit': 10, 'credit': 0},
                    {'account': 'other_revenue', 'debit': 0, 'credit': 10}])
        self.assertEqual(je.date, _dt.date(2026, 10, 1))

    # ── phones (robot / kiosk / website orders) ──────────────────────
    def test_phone_lookup_matches_the_stored_normalized_form(self):
        c1, _ = Customer.get_or_create_by_phone('0000000000', defaults={'name': 'عميل نقدي'})
        c2, created = Customer.get_or_create_by_phone('0000000000', defaults={'name': 'عميل نقدي'})
        self.assertEqual((c1.pk, created), (c2.pk, False))     # 2nd walk-in sale used to crash
        saeed = Customer.objects.create(name='سعيد', phone='01012345678')
        for raw in ('01012345678', '+201012345678', '201012345678', '010 1234 5678'):
            self.assertEqual(Customer.find_by_phone(raw).pk, saeed.pk, raw)

    def test_robot_backorder_is_saved_as_an_order_without_touching_stock(self):
        from robot.services import create_robot_sale
        inv = create_robot_sale(product=self.product, branch=self.branch, customer=self.customer,
                                quantity=80, post=False)
        self.assertEqual(inv.status, 'quotation')
        self.assertEqual(inv.total_amount, D('8000.00'))
        self.assertEqual(self._stock(), 50)
        self.cash.refresh_from_db()
        self.assertEqual(self.cash.balance, D('5000.00'))

    # ── payroll ───────────────────────────────────────────────────────
    def test_payroll_pays_from_a_funded_treasury_under_the_salaries_item(self):
        from types import SimpleNamespace
        from django.core.exceptions import ValidationError
        from django.db import transaction
        from inventory.models import FinancialTransaction
        from hr.services.payroll_service import PayrollService
        user, profile = make_employee(username='emp_pay', role='cashier', branch=self.branch)
        empty = make_treasury(self.branch, name='خزنة فاضية', balance='0.00')
        with transaction.atomic():
            tx_id = PayrollService._create_treasury_transaction(
                'automotive', SimpleNamespace(user=user), D('3000'), 'راتب 9/2026')
        ft = FinancialTransaction.objects.get(pk=tx_id)
        self.assertEqual(ft.treasury_id, self.cash.pk)          # not the empty one
        self.assertEqual(ft.category.system_key, 'salaries')
        self.assertEqual(acct(AccountingService.category_account_code(ft.category)), D('3000.00'))
        self.assertEqual(acct('5099'), D('0.00'))
        with self.assertRaises(ValidationError):
            with transaction.atomic():
                PayrollService._create_treasury_transaction(
                    'automotive', SimpleNamespace(user=user), D('99999'), 'راتب كبير')
        empty.refresh_from_db()
        self.assertEqual(empty.balance, D('0.00'))

    # ── customer statement ────────────────────────────────────────────
    def test_customer_statement_nets_refunds_and_counts_payments_on_account(self):
        si = make_sale_invoice(self.customer, self.branch, treasury=self.cash,
                               items=[(self.product, 2, '100.00')], paid_amount='200.00')
        self._post(si)
        self._post(InvoiceService.create_return_invoice(si))          # refunded 200
        credit = self._post(make_sale_invoice(self.customer, self.branch,
                                              items=[(self.product, 1, '100.00')]))
        self.assertEqual(credit.due_amount, D('100.00'))
        self._client().post(f'/system/customers/{self.customer.pk}/collect/',
                            {'treasury_id': self.cash.pk, 'amount': '60'}, HTTP_HOST=self.host)
        r = self._client().get(f'/system/reports/customers/{self.customer.pk}/?period=all',
                               HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context['net_sales'], D('100.00'))
        self.assertEqual(r.context['paid'], D('60.00'))       # 200 paid − 200 refunded + 60 on account
        self.assertEqual(r.context['due'], D('40.00'))
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.balance, D('40.00'))   # statement agrees with the balance

    def test_cashier_cannot_unbill_lines_on_the_review_screen(self):
        cashier, _profile = make_employee(username='cashier_rv2', role='cashier', branch=self.branch)
        si = make_sale_invoice(self.customer, self.branch, items=[(self.product, 1, '100.00')])
        c = Client()
        c.force_login(cashier)
        r = c.post(f'/system/invoice/{si.pk}/review/', {}, HTTP_HOST=self.host)   # untick everything
        self.assertEqual(r.status_code, 403)
        si.refresh_from_db()
        self.assertEqual(si.total_amount, D('100.00'))
        self.assertTrue(si.items.get().is_billable)

    def test_statement_apis_treat_returns_as_returns(self):
        from inventory.services.purchase_return_service import return_to_vendor
        si = make_sale_invoice(self.customer, self.branch, treasury=self.cash,
                               items=[(self.product, 2, '100.00')], paid_amount='200.00')
        self._post(si)
        self._post(InvoiceService.create_return_invoice(si))
        r = self._client().get(f'/system/api/v1/statement/customer/{self.customer.pk}/', HTTP_HOST=self.host)
        body = json.loads(r.content)
        self.assertEqual([e['type'] for e in body['entries']], ['invoice', 'return'])
        self.assertEqual(body['entries'][-1]['balance'], 0.0)          # was +200 after the "return"
        self.assertEqual(body['totals']['total_paid'], 0.0)

        r = self._client().post('/system/purchases/save/', data=json.dumps({
            'branch_id': self.branch.pk, 'vendor_id': self.vendor.pk,
            'items': [{'product_id': self.product.pk, 'qty': 4, 'cost': 50}],
        }), content_type='application/json', HTTP_HOST=self.host)
        pi = PurchaseInvoice.objects.get(pk=json.loads(r.content)['invoice_id'])
        return_to_vendor(pi, [(pi.items.get().pk, 1)])
        self.vendor.refresh_from_db()
        r = self._client().get(f'/system/api/v1/statement/vendor/{self.vendor.pk}/', HTTP_HOST=self.host)
        body = json.loads(r.content)
        self.assertEqual(body['entries'][-1]['balance'], float(self.vendor.balance))   # 150
        self.assertEqual(body['totals']['total_returned'], 50.0)
