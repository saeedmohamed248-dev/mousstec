"""
🏛️ الدورة المحاسبية لقطع الغيار لحد آخرها: إقفال الشهور · سداد ض.ق.م · دفتر الأستاذ.

اللي اتلقى في المراجعة الشاملة:
* الإقفال كان من لوحة الأدمن الخام بس — من غير سنة مالية جاهزة، والإجراء بيقفل
  أي فترة حتى الشهر الحالي (فكل بيع النهارده يترفض) أو شهر وقبله شهر مفتوح؛
* الضريبة اللي بتتحصّل على الفواتير بتتراكم في «ض.ق.م المستحقة» (٢٢٠٠) ومفيش
  أي طريقة لسدادها غير إنها تتسجّل مصروف — فالربح يقل بالغلط؛
* ميزان المراجعة مالوش تفاصيل لكل حساب (دفتر الأستاذ كان API بس)؛
* تقرير المبيعات كان بيحسب الضريبة ربح (الربح = الصافي شامل الضريبة − التكلفة).
"""
import json
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.db import connection, transaction
from django.db.models import Sum
from django.test import Client, override_settings
from django.utils import timezone

from inventory.models import (
    AccountingEntry, AccountingPeriod, Customer, FinancialTransaction, FiscalYear, JournalEntry,
    SaleInvoice, ServiceCatalog, Treasury,
)
from inventory.services.accounting_service import AccountingService, period_close_problem
from inventory.services.inventory_service import set_branch_stock
from .base import ERPTenantTestCase
from .factories import make_branch, make_employee, make_product

D = lambda v: Decimal(str(v))  # noqa: E731


def acct(code):
    agg = AccountingEntry.objects.filter(account__code=code).aggregate(d=Sum('debit'), c=Sum('credit'))
    return (agg['d'] or D(0)) - (agg['c'] or D(0))


@override_settings(ALLOWED_HOSTS=['*'])
class AccountingCenterTests(ERPTenantTestCase):

    def setUp(self):
        connection.set_tenant(self.tenant)
        self.host = self.domain.domain
        self.branch = make_branch('الرئيسي')
        self.cash = Treasury.objects.create(name='كاش', branch=self.branch, balance=D('0'), is_active=True)
        FinancialTransaction.objects.create(treasury=self.cash, transaction_type='in', amount=D('5000'),
                                            description='رأس مال', equity_kind='capital')
        self.oil = make_product(part_number='AC-OIL', name='زيت', retail_price='1250',
                                purchase_price='900', average_cost='900')
        with transaction.atomic():
            set_branch_stock(self.oil, self.branch, 10, unit_cost=D('900'), reference_type='Test', opening=True)
        self.customer = Customer.objects.create(name='عميل الضريبة', phone='01012340000')
        self.service = ServiceCatalog.objects.create(name='تغيير زيت', labor_price=D('150'))
        self.owner = User.objects.create_user('ac_owner', password='x', is_staff=True, is_superuser=True)
        self.c = Client()
        self.c.force_login(self.owner)

    # ── helpers ───────────────────────────────────────────────────────
    def post(self, url, data=None, client=None):
        return (client or self.c).post(url, data or {}, HTTP_HOST=self.host)

    def get(self, url, client=None, **params):
        return (client or self.c).get(url, params, HTTP_HOST=self.host)

    def taxed_job(self):
        """أمر شغل: زيت 1250 + خدمة 150 + مصنعية 100 = 1500 + ض.ق.م 14٪ (210) بتكلفة 900."""
        r = self.c.post('/system/job-card/save/', data=json.dumps({
            'branch_id': self.branch.pk, 'customer_id': self.customer.pk,
            'vehicle_chassis': 'WBAACCT0000000001', 'vehicle_model': '320i',
            'invoice_type': 'maintenance', 'labor_cost_manual': '100', 'tax_percentage': '14',
            'items': [{'product_id': self.oil.pk, 'qty': 1, 'price': 1250}],
            'services': [{'service_id': self.service.pk}]}),
            content_type='application/json', HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 200, r.content)
        job = SaleInvoice.objects.get(pk=r.json()['invoice_id'])
        self.post(f'/system/invoices/{job.pk}/deliver/')
        job.refresh_from_db()
        self.assertEqual((job.status, job.total_amount), ('posted', D('1710.00')))
        return job

    def make_year(self, year):
        r = self.post('/system/accounting/periods/', {'action': 'create_year', 'year': str(year)})
        self.assertIn('ok=year', r['Location'])

    # ── تقرير المبيعات ────────────────────────────────────────────────
    def test_sales_report_profit_and_margin_exclude_vat(self):
        self.taxed_job()
        ctx = self.get('/system/reports/sales/', period='month').context
        self.assertEqual(ctx['net_sales'], D('1710.00'))     # اللي العميل بيدفعه
        self.assertEqual(ctx['vat_amount'], D('210.00'))
        self.assertEqual(ctx['profit'], D('600.00'))         # كان 810 (الضريبة كلها ربح)
        self.assertEqual(round(ctx['margin'], 2), D('40.00'))
        pnl = self.get('/system/reports/pnl/', period='month').context
        self.assertEqual(pnl['gross'], ctx['profit'])

    # ── الإقفال الشهري ────────────────────────────────────────────────
    def test_year_is_created_and_months_close_in_order_only_after_they_end(self):
        today = timezone.localdate()
        prev_month_end = today.replace(day=1) - timedelta(days=1)
        for year in sorted({prev_month_end.year, today.year}):
            self.make_year(year)
        r = self.post('/system/accounting/periods/', {'action': 'create_year', 'year': str(today.year)})
        self.assertIn('err=year_exists', r['Location'])
        self.assertEqual(AccountingPeriod.objects.filter(fiscal_year__start_date__year=today.year).count(), 12)

        current = AccountingPeriod.for_date(today)
        r = self.post('/system/accounting/periods/', {'action': 'close', 'period': current.pk})
        self.assertIn('err=not_ended', r['Location'])
        self.assertFalse(AccountingPeriod.objects.filter(is_closed=True).exists())

        # «اقفل لحد الشهر اللي فات» بيقفل كل اللي قبله بالترتيب — والشهر الحالي يفضل شغال
        prev = AccountingPeriod.for_date(prev_month_end)
        r = self.post('/system/accounting/periods/', {'action': 'close', 'period': prev.pk})
        self.assertIn('ok=closed', r['Location'])
        self.assertFalse(AccountingPeriod.objects.filter(end_date__lte=prev.end_date, is_closed=False).exists())
        self.assertFalse(AccountingPeriod.objects.get(pk=current.pk).is_closed)
        self.taxed_job()   # البيع النهارده لسه بيتقيّد عادي
        self.assertTrue(JournalEntry.objects.filter(journal_type='sales').exists())

        # مدير/محاسب مايعيدش الفتح — والمالك من الأحدث للأقدم
        _u, _p = make_employee('ac_accountant', role='accountant', branch=self.branch)
        acc = Client()
        acc.force_login(User.objects.get(username='ac_accountant'))
        r = self.post('/system/accounting/periods/', {'action': 'reopen', 'period': prev.pk}, client=acc)
        self.assertEqual(r.status_code, 403)
        older = AccountingPeriod.objects.filter(end_date__lt=prev.start_date).order_by('-start_date').first()
        if older is not None:
            r = self.post('/system/accounting/periods/', {'action': 'reopen', 'period': older.pk})
            self.assertIn('err=reopen_order', r['Location'])
        r = self.post('/system/accounting/periods/', {'action': 'reopen', 'period': prev.pk})
        self.assertIn('ok=reopened', r['Location'])
        self.assertFalse(AccountingPeriod.objects.get(pk=prev.pk).is_closed)
        self.assertFalse(JournalEntry.objects.filter(reference__in=[f'CLOSE-{prev.pk}', f'CLOSE-RE-{prev.pk}']).exists())

    def test_close_guard_used_by_admin_action(self):
        today = timezone.localdate()
        fy = FiscalYear.objects.create(code='G', name='G', start_date=date(today.year - 1, 1, 1),
                                       end_date=date(today.year, 12, 31))
        jan = AccountingPeriod.objects.create(fiscal_year=fy, name='jan', start_date=date(today.year - 1, 1, 1),
                                              end_date=date(today.year - 1, 1, 31))
        feb = AccountingPeriod.objects.create(fiscal_year=fy, name='feb', start_date=date(today.year - 1, 2, 1),
                                              end_date=date(today.year - 1, 2, 28))
        now = AccountingPeriod.objects.create(fiscal_year=fy, name='now', start_date=today.replace(day=1),
                                              end_date=today)
        self.assertEqual(period_close_problem(now), "مينفعش تقفل فترة لسه ماخلصتش.")
        self.assertEqual(period_close_problem(feb), "اقفل الفترات اللي قبلها الأول بالترتيب.")
        self.assertEqual(period_close_problem(jan), '')

        from django.contrib.admin.sites import AdminSite
        from django.contrib.messages.storage.fallback import FallbackStorage
        from django.test import RequestFactory
        from inventory.admin.accounting import AccountingPeriodAdmin
        request = RequestFactory().post('/')
        request.user = self.owner
        request.session = {}
        request._messages = FallbackStorage(request)
        admin = AccountingPeriodAdmin(AccountingPeriod, AdminSite())
        admin.close_selected_periods(request, AccountingPeriod.objects.filter(pk__in=[now.pk, feb.pk, jan.pk]))
        closed = set(AccountingPeriod.objects.filter(is_closed=True).values_list('name', flat=True))
        self.assertEqual(closed, {'jan', 'feb'})   # بالترتيب — والشهر الحالي اترفض

    # ── ضريبة القيمة المضافة ──────────────────────────────────────────
    def test_vat_payment_settles_the_liability_and_is_not_an_expense(self):
        self.taxed_job()
        self.assertEqual(-acct('2200'), D('210.00'))
        ctx = self.get('/system/accounting/').context
        self.assertEqual(ctx['vat_due'], D('210.00'))
        pnl_before = self.get('/system/reports/pnl/', period='month').context['net_profit']

        r = self.post('/system/accounting/vat/pay/', {'treasury_id': self.cash.pk, 'amount': '211'})
        self.assertIn('err=over', r['Location'])          # أكتر من اللي اتحصّل
        r = self.post('/system/accounting/vat/pay/', {'treasury_id': self.cash.pk, 'amount': '210',
                                                      'description': 'إقرار الشهر'})
        self.assertIn('ok=vat_paid', r['Location'])
        self.cash.refresh_from_db()
        self.assertEqual(self.cash.balance, D('5000') - D('210'))
        self.assertEqual(acct('2200'), D('0.00'))
        self.assertEqual(acct('1001'), self.cash.balance)
        ft = FinancialTransaction.objects.get(equity_kind='vat')
        self.assertEqual(ft.description, 'سداد ضريبة القيمة المضافة — إقرار الشهر')
        # مش مصروف: لا في قائمة الدخل ولا في المصاريف ولا في الدفتر
        self.assertEqual(self.get('/system/reports/pnl/', period='month').context['net_profit'], pnl_before)
        self.assertNotIn('إقرار الشهر', self.get('/system/expenses/').content.decode())
        self.assertFalse(AccountingEntry.objects.filter(financial_transaction=ft,
                                                        account__account_type='expense').exists())
        # حذف السداد الغلط من كشف الخزنة بيرجّع الالتزام والفلوس
        r = self.post(f'/system/transactions/{ft.pk}/delete/')
        self.assertIn('ok=deleted', r['Location'])
        self.cash.refresh_from_db()
        self.assertEqual((self.cash.balance, -acct('2200')), (D('5000'), D('210.00')))

    def test_vat_payment_needs_funds_and_a_finance_role(self):
        self.taxed_job()
        poor = Treasury.objects.create(name='فاضية', branch=self.branch, balance=D('0'), is_active=True)
        r = self.post('/system/accounting/vat/pay/', {'treasury_id': poor.pk, 'amount': '100'})
        self.assertIn('err=balance', r['Location'])
        cashier, _p = make_employee('ac_cashier', role='cashier', branch=self.branch)
        cl = Client()
        cl.force_login(cashier)
        r = self.post('/system/accounting/vat/pay/', {'treasury_id': self.cash.pk, 'amount': '100'}, client=cl)
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.get('/system/accounting/', client=cl).status_code, 403)
        self.assertFalse(FinancialTransaction.objects.filter(equity_kind='vat').exists())

    # ── دفتر الأستاذ ──────────────────────────────────────────────────
    def test_account_ledger_runs_the_balance_and_links_the_source(self):
        job = self.taxed_job()
        today = timezone.localdate().isoformat()
        r = self.get('/system/accounting/ledger/1100/', **{'from': '2000-01-01', 'to': today})
        self.assertEqual(r.status_code, 200)
        rows = r.context['rows']
        self.assertEqual(rows[-1]['balance'], D('1710.00'))
        self.assertEqual(r.context['closing'], acct('1100'))
        self.assertTrue(any(f'/system/invoice/{job.pk}/print/a4/' == row['url'] for row in rows))
        tb = self.get('/system/reports/trial-balance/')
        self.assertIn('/system/accounting/ledger/1100/', tb.content.decode())
        r = self.get('/system/accounting/ledger/1100/', **{'from': '2000-01-01', 'to': today, 'export': 'xlsx'})
        self.assertEqual(r.status_code, 200)
        # حساب مش موجود ← صفحة «مش موجود» العامة (تحويل) مش خطأ 500
        self.assertIn(self.get('/system/accounting/ledger/9999999/').status_code, (302, 404))
