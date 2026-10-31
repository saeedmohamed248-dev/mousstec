"""
📒 تكملة دورات المطبعة المحاسبية — من غير أي دورة ناقصة.

* إشعار دائن (مرتجع/خصم بعد التسليم): الإيراد والضريبة بيتعكسوا والعميل مديونيته تنزل.
* أصول ثابتة: الشراء أصل مش مصروف، قسط إهلاك شهري، والبيع/الاستبعاد بربح أو خسارة.
* فواتير مصروفات (إيجار/كهرباء…) مستحقة في شهرها على حساب المورد وتتسدّد بعدين.
* مرتجع مشتريات جزئي: الخامات تخرج بتكلفة الفاتورة والمستحق للمورد ينزل.
* تحويل بين الخزائن + حساب لكل خزنة/بنك + مطابقة البنك مع الكشف.
* إقرار ضريبة القيمة المضافة + أعمار الديون (عملاء وموردين).
* إقفال الفترات وإعادة فتحها + استحقاق المرتبات في شهرها.
* كل سيناريو: ميزان المراجعة متزن، وكل خزنة = حسابها في الدفتر، والمخزون = قيمة الخامات.
"""
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import connection
from django.db.models import Q, Sum
from django.test import Client, override_settings
from django.utils import timezone

from inventory.models import AccountingEntry, AccountingPeriod, FiscalYear, JournalEntry
from inventory.services.accounting_reports import AccountingReportService
from inventory.tests.base import ERPTenantTestCase
from printing import ledger
from printing.models import (
    MachineProfile, MaterialPurchase, MaterialPurchaseLine, MaterialPurchaseReturn, MaterialPurchaseReturnLine,
    PrintCreditNote, PrintCustomer, PrintExpenseBill, PrintFixedAsset, PrintJob, PrintJobMaterial,
    PrintLedgerState, PrintMaterial, PrintOrder, PrintSupplier, PrintTransaction, PrintTreasury,
    PrintTreasuryTransfer, PrintVatReturn,
)
from printing.views.accounting import payables_aging, receivables_aging

User = get_user_model()
D = Decimal


def bal(code):
    """رصيد الحساب في الدفتر (مدين − دائن) — شامل حساباته الفرعية."""
    agg = (AccountingEntry.objects.filter(Q(account__code=code) | Q(account__parent__code=code))
           .aggregate(d=Sum('debit'), c=Sum('credit')))
    return (agg['d'] or D('0')) - (agg['c'] or D('0'))


CASH, BANK, AR, INVENTORY, VAT_IN, SUP_ADV = '1001', '1002', '1100', '1200', '1250', '1260'
FIXED, ACC_DEP, DISPOSAL = '1300', '1390', '1395'
AP, SAL_PAYABLE, VAT_OUT, VAT_DUE, DEPOSITS, OPENING = '2100', '2150', '2200', '2210', '2400', '3005'
REVENUE, RETURNS, GAIN = '4010', '4090', '4095'
SALARIES, RENT, DEPRECIATION, LOSS = '5100', '5201', '5600', '5650'


@override_settings(ALLOWED_HOSTS=['*'])
class PrintingAccountingCompletionTests(ERPTenantTestCase):

    def setUp(self):
        super().setUp()
        connection.set_tenant(self.tenant)
        type(self.tenant).objects.filter(pk=self.tenant.pk).update(industry='printing')
        self.host = self.domain.domain
        self.treasury = PrintTreasury.objects.create(name='الخزنة الرئيسية', balance=D('50000'))
        self.customer = PrintCustomer.objects.create(name='شركة الأمل', phone='01011112222')
        self.supplier = PrintSupplier.objects.create(name='مورد الورق', phone='01099998888')
        self.landlord = PrintSupplier.objects.create(name='صاحب المحل', phone='01077776666')
        self.paper = PrintMaterial.objects.create(name='ورق كوشيه', unit='فرخ', quantity=D('10'),
                                                  cost_per_unit=D('2'))
        self.user = User.objects.create_superuser('closing_boss', 'c@x.com', 'x')
        self.c = Client()
        self.c.force_login(self.user)
        self.today = timezone.localdate()

    def tearDown(self):
        self.assert_books_balance()
        super().tearDown()

    # ── helpers ────────────────────────────────────────────────────────
    def assert_books_balance(self):
        tb = AccountingReportService.trial_balance()
        self.assertTrue(tb['is_balanced'], tb)
        self.assertEqual(bal(CASH) + bal(BANK), sum(t.balance for t in PrintTreasury.objects.all()))
        for t in PrintTreasury.objects.all():
            self.assertEqual(ledger.treasury_account(t).balance, t.balance, t.name)
        stock = sum((m.quantity * m.cost_per_unit for m in PrintMaterial.objects.all()), D('0'))
        self.assertLess(abs(bal(INVENTORY) - stock), D('1.00'))

    def get(self, url, **kw):
        return self.c.get(url, HTTP_HOST=self.host, **kw)

    def post(self, url, data):
        return self.c.post(url, data, HTTP_HOST=self.host)

    def _delivered_order(self, price='1000', tax='0', paid=None):
        order = PrintOrder.objects.create(customer=self.customer, status='confirmed', tax_amount=D(tax))
        PrintJob.objects.create(order=order, description='كتالوج', quantity=1, unit_price=D(price), proof_waived=True)
        if paid:
            PrintTransaction.objects.create(treasury=self.treasury, transaction_type='in', amount=D(paid),
                                            order=order, created_by=self.user)
        order.refresh_from_db()
        order.status = 'delivered'
        order.save()
        order.refresh_from_db()
        return order

    def _received_purchase(self, qty='100', cost='3', tax='0', terms='credit'):
        p = MaterialPurchase.objects.create(supplier=self.supplier, payment_terms=terms, tax_amount=D(tax),
                                            treasury=self.treasury if terms == 'cash' else None,
                                            created_by=self.user)
        MaterialPurchaseLine.objects.create(purchase=p, material=self.paper, quantity=D(qty), unit_cost=D(cost))
        p.refresh_from_db()
        p.receive(user=self.user)
        p.refresh_from_db()
        return p

    def _months_ago(self, n):
        d = self.today.replace(day=1)
        for _i in range(n):
            d = (d - timedelta(days=1)).replace(day=1)
        return d

    # ── 1. الإشعار الدائن (مرتجع بعد التسليم) ──────────────────────────
    def test_credit_note_reverses_revenue_vat_and_refunds_cash(self):
        order = self._delivered_order(price='1000', tax='140', paid='1140')
        self.assertEqual(bal(REVENUE), D('-1000.00'))
        cn = PrintCreditNote.objects.create(order=order, reason='100 كارت بعيب طباعة', amount=D('100'),
                                            tax_amount=D('14'), refund_treasury=self.treasury, created_by=self.user)
        order.refresh_from_db()
        self.assertEqual(order.credited_amount, D('114.00'))
        self.assertEqual(order.remaining, D('0.00'))
        self.assertEqual(bal(RETURNS), D('100.00'))              # مردودات المبيعات (تقلّل الإيراد)
        self.assertEqual(bal(VAT_OUT), D('-126.00'))
        self.assertEqual(bal(AR), D('0.00'))
        self.assertEqual(bal(DEPOSITS), D('0.00'))
        self.treasury.refresh_from_db()
        self.assertEqual(self.treasury.balance, D('50000') + D('1140') - D('114'))
        self.assertEqual(cn.refund_txn.amount, D('114.00'))
        pnl = AccountingReportService.income_statement(self.today.replace(day=1), self.today)
        self.assertEqual(pnl['revenue']['total'], D('900.00'))

        # مسح الإشعار: الفلوس ترجع الخزنة والقيود تتشال
        cn.delete()
        order.refresh_from_db()
        self.assertEqual(order.credited_amount, D('0'))
        self.assertEqual(bal(RETURNS), D('0'))
        self.treasury.refresh_from_db()
        self.assertEqual(self.treasury.balance, D('51140.00'))
        self.assertFalse(PrintTransaction.objects.filter(transaction_type='out', order=order).exists())

    def test_credit_note_without_refund_becomes_customer_credit(self):
        order = self._delivered_order(price='1000', paid='1000')
        PrintCreditNote.objects.create(order=order, reason='خصم لاحق', amount=D('200'), created_by=self.user)
        self.assertEqual(self.customer.account_summary()['credit'], D('200.00'))
        self.assertEqual(bal(AR), D('0.00'))
        self.assertEqual(bal(DEPOSITS), D('-200.00'))           # رصيد مقدّم للعميل (التزام)

    def test_credit_note_refused_before_delivery_or_above_order(self):
        order = PrintOrder.objects.create(customer=self.customer, status='confirmed')
        PrintJob.objects.create(order=order, description='بنر', quantity=1, unit_price=D('500'), proof_waived=True)
        with self.assertRaises(ValidationError):
            PrintCreditNote.objects.create(order=order, reason='x', amount=D('10'))
        order = self._delivered_order(price='300')
        with self.assertRaises(ValidationError):
            PrintCreditNote.objects.create(order=order, reason='x', amount=D('301'))
        self.assertFalse(PrintCreditNote.objects.exists())

    # ── 2. الأصول الثابتة ──────────────────────────────────────────────
    def test_cash_fixed_asset_is_capitalised_and_depreciated_monthly(self):
        start = self._months_ago(3)
        asset = PrintFixedAsset.objects.create(name='ماكينة ديجيتال', cost=D('12000'), useful_life_months=12,
                                               purchase_date=start, funding='cash', treasury=self.treasury,
                                               created_by=self.user)
        self.treasury.refresh_from_db()
        self.assertEqual(self.treasury.balance, D('38000.00'))
        self.assertEqual(PrintTransaction.objects.get(fixed_asset=asset).category, 'fixed_asset')
        self.assertEqual(bal(FIXED), D('12000.00'))              # أصل مش مصروف
        accumulated = asset.accumulated_until(self.today)
        self.assertGreaterEqual(accumulated, D('3000.00'))       # 3 شهور خلصوا على الأقل
        self.assertEqual(bal(ACC_DEP), -accumulated)
        self.assertEqual(bal(DEPRECIATION), accumulated)
        self.assertEqual(asset.book_value(self.today), D('12000') - accumulated)

        # إعادة التشغيل مابتكرّرش قيود
        ledger.ensure_depreciation(rebuild_from=start)
        ledger.ensure_bootstrapped()
        self.assertEqual(bal(DEPRECIATION), accumulated)

        # البيع: يقفل التكلفة ومجمعها، والفرق ربح/خسارة
        asset.disposed_on = self.today
        asset.disposal_amount = D('10000')
        asset.disposal_treasury = self.treasury
        asset.save()
        # 🐛 [FIX]: شهر البيع مالوش إهلاك — لو البيع آخر يوم في الشهر قسط الشهر ده (اللي اتقيّد الصبح)
        #    بيتشال، فالربح بيتحسب من المتراكم بعد البيع (الاختبار كان بيقع آخر يوم في أي شهر).
        accumulated = asset.accumulated_until(self.today)
        gain = D('10000') - (D('12000') - accumulated)
        self.assertEqual(bal(FIXED), D('0'))
        self.assertEqual(bal(ACC_DEP), D('0'))
        self.assertEqual(bal(DISPOSAL), D('0'))
        self.assertEqual(bal(GAIN) if gain > 0 else -bal(LOSS), -gain if gain > 0 else gain)
        self.treasury.refresh_from_db()
        self.assertEqual(self.treasury.balance, D('48000.00'))

        # البيع مابيتعدّلش، والتكلفة النقدية مابتتغيّرش
        asset.refresh_from_db()
        asset.disposal_amount = D('1')
        with self.assertRaises(ValidationError):
            asset.save()

    def test_scrapping_asset_books_loss_and_delete_returns_money(self):
        asset = PrintFixedAsset.objects.create(name='مكنة قص', cost=D('6000'), useful_life_months=60,
                                               purchase_date=self.today, funding='cash', treasury=self.treasury)
        asset.cost = D('5000')
        with self.assertRaises(ValidationError):
            asset.save()
        asset.refresh_from_db()
        asset.disposed_on = self.today
        asset.save()                                              # استبعاد من غير مبلغ
        self.assertEqual(bal(LOSS), D('6000.00'))
        self.assertEqual(bal(FIXED), D('0'))
        with self.assertRaises(ValidationError):
            PrintTransaction.objects.get(fixed_asset=asset).delete()   # ثمن الأصل لوحده مابيتمسحش
        asset.delete()
        self.treasury.refresh_from_db()
        self.assertEqual(self.treasury.balance, D('50000.00'))
        self.assertEqual(bal(LOSS), D('0'))
        self.assertEqual(bal(FIXED), D('0'))

    def test_credit_and_opening_fixed_assets(self):
        asset = PrintFixedAsset.objects.create(name='بلوتر', cost=D('8000'), funding='credit',
                                               supplier=self.supplier, purchase_date=self.today)
        self.assertEqual(bal(AP), D('-8000.00'))
        self.assertEqual(self.supplier.account_summary()['balance'], D('8000.00'))
        PrintTransaction.objects.create(treasury=self.treasury, transaction_type='out', amount=D('8000'),
                                        supplier=self.supplier, created_by=self.user)
        self.assertEqual(bal(AP), D('0.00'))
        self.assertEqual(self.supplier.account_summary()['balance'], D('0.00'))
        self.assertEqual(bal(FIXED), D('8000.00'))
        asset.delete()

        old = PrintFixedAsset.objects.create(name='ماكينة أوفست قديمة', cost=D('50000'), funding='opening',
                                             opening_accumulated=D('20000'), useful_life_months=30,
                                             purchase_date=date(self.today.year - 3, 1, 1),
                                             # 🐛 [FIX]: من أول الشهر الجاي — آخر يوم في الشهر قسط الشهر الحالي
                                             #    بيتقيّد (21,000 بدل 20,000) فالاختبار كان بيقع.
                                             depreciation_start=(self.today.replace(day=28)
                                                                 + timedelta(days=4)).replace(day=1))
        self.assertEqual(bal(FIXED), D('50000.00'))
        self.assertEqual(bal(ACC_DEP), D('-20000.00'))
        self.assertEqual(old.book_value(self.today), D('30000.00'))

    # ── 3. فواتير المصروفات ─────────────────────────────────────────────
    def test_expense_bill_accrues_then_paid_then_cancelled(self):
        bill = PrintExpenseBill.objects.create(supplier=self.landlord, category='rent', description='إيجار الشهر',
                                               amount=D('3000'), date=self.today, created_by=self.user)
        self.assertEqual(bal(RENT), D('3000.00'))                # المصروف في شهره من غير ما يتدفع
        self.assertEqual(bal(AP), D('-3000.00'))
        self.assertEqual(self.landlord.account_summary()['balance'], D('3000.00'))
        txn = PrintTransaction.objects.create(treasury=self.treasury, transaction_type='out', amount=D('3000'),
                                              bill=bill, created_by=self.user)
        self.assertEqual(txn.category, 'supplier')
        self.assertEqual(txn.supplier_id, self.landlord.pk)
        bill.refresh_from_db()
        self.assertEqual(bill.paid_amount, D('3000.00'))
        self.assertEqual(bill.remaining, D('0.00'))
        self.assertEqual(bal(AP), D('0.00'))
        self.assertEqual(bal(RENT), D('3000.00'))                 # السداد مش مصروف تاني

        bill.cancel()
        self.assertEqual(bal(RENT), D('0'))
        self.assertEqual(bal(SUP_ADV), D('3000.00'))              # اللي اتدفع بقى رصيد لينا عند المورد
        self.assertEqual(self.landlord.account_summary()['balance'], D('-3000.00'))

    def test_cash_expense_bill_pays_from_treasury(self):
        bill = PrintExpenseBill.objects.create(supplier=self.landlord, category='utilities', description='كهرباء',
                                               amount=D('800'), tax_amount=D('112'), payment_terms='cash',
                                               treasury=self.treasury)
        bill.refresh_from_db()
        self.assertEqual(bill.paid_amount, D('912.00'))
        self.assertEqual(bal(VAT_IN), D('112.00'))
        self.assertEqual(bal(AP), D('0.00'))
        self.treasury.refresh_from_db()
        self.assertEqual(self.treasury.balance, D('49088.00'))

    # ── 4. مرتجع المشتريات الجزئي ──────────────────────────────────────
    def test_partial_purchase_return_reduces_stock_payable_and_vat(self):
        p = self._received_purchase(qty='100', cost='3', tax='42')
        self.assertEqual(bal(AP), D('-342.00'))
        ret = MaterialPurchaseReturn.objects.create(purchase=p, reason='ورق مقطوع', created_by=self.user)
        MaterialPurchaseReturnLine.objects.create(ret=ret, purchase_line=p.lines.get(), quantity=D('40'))
        ret.post(user=self.user)
        self.assertEqual(ret.status, 'posted')
        self.assertEqual(ret.value, D('120.00'))
        self.assertEqual(ret.tax_amount, D('16.80'))
        self.paper.refresh_from_db()
        self.assertEqual(self.paper.quantity, D('70'))
        self.assertEqual(bal(AP), D('-205.20'))
        self.assertEqual(bal(VAT_IN), D('25.20'))
        p.refresh_from_db()
        self.assertEqual(p.remaining, D('205.20'))
        self.assertEqual(self.supplier.account_summary()['balance'], D('205.20'))

        # أكتر من الباقي في الفاتورة مرفوض، والإلغاء بعد مرتجع مرفوض، والمترحّل مابيتمسحش
        ret2 = MaterialPurchaseReturn.objects.create(purchase=p)
        MaterialPurchaseReturnLine.objects.create(ret=ret2, purchase_line=p.lines.get(), quantity=D('61'))
        with self.assertRaises(ValidationError):
            ret2.post()
        with self.assertRaises(ValidationError):
            p.cancel()
        with self.assertRaises(ValidationError):
            ret.delete()

    def test_purchase_return_with_cash_refund(self):
        p = self._received_purchase(qty='50', cost='4', terms='cash')
        self.treasury.refresh_from_db()
        self.assertEqual(self.treasury.balance, D('49800.00'))
        ret = MaterialPurchaseReturn.objects.create(purchase=p, refund_treasury=self.treasury)
        MaterialPurchaseReturnLine.objects.create(ret=ret, purchase_line=p.lines.get(), quantity=D('10'))
        ret.post()
        self.treasury.refresh_from_db()
        self.assertEqual(self.treasury.balance, D('49840.00'))
        self.assertEqual(bal(AP), D('0.00'))
        self.assertEqual(self.supplier.account_summary()['balance'], D('0.00'))
        p.refresh_from_db()
        self.assertEqual(p.remaining, D('0.00'))

    # ── 5. التحويلات + حساب لكل خزنة + مطابقة البنك ──────────────────────
    def test_treasury_transfer_moves_cash_between_own_accounts(self):
        bank = PrintTreasury.objects.create(name='بنك مصر', kind='bank')
        self.assertTrue(ledger.treasury_account(bank).code.startswith('1002-'))
        self.assertTrue(ledger.treasury_account(self.treasury).code.startswith('1001-'))
        transfer = PrintTreasuryTransfer.objects.create(from_treasury=self.treasury, to_treasury=bank,
                                                        amount=D('20000'), created_by=self.user)
        bank.refresh_from_db()
        self.assertEqual(bank.balance, D('20000.00'))
        self.assertEqual(bal('1090'), D('0'))
        self.assertEqual(bal(BANK), D('20000.00'))
        with self.assertRaises(ValidationError):
            transfer.out_txn.delete()                              # رجل واحدة مابتتمسحش
        transfer.delete()
        bank.refresh_from_db()
        self.treasury.refresh_from_db()
        self.assertEqual((bank.balance, self.treasury.balance), (D('0'), D('50000.00')))
        self.assertFalse(PrintTransaction.objects.filter(category='transfer').exists())
        with self.assertRaises(ValidationError):
            PrintTreasuryTransfer.objects.create(from_treasury=bank, to_treasury=self.treasury, amount=D('1'))

    def test_changing_treasury_kind_moves_its_balance_to_the_bank_account(self):
        PrintTransaction.objects.create(treasury=self.treasury, transaction_type='out', amount=D('500'),
                                        category='rent')
        self.treasury.kind = 'wallet'
        self.treasury.save()
        acc = ledger.treasury_account(self.treasury)
        self.assertTrue(acc.code.startswith('1002-'))
        self.assertEqual(acc.balance, D('49500.00'))
        self.assertEqual(bal(CASH), D('0'))
        # التدفق النقدي بيشوف حسابات الخزن الفرعية
        flow = AccountingReportService.cash_flow(self.today - timedelta(days=1), self.today)
        self.assertEqual(flow['closing_cash'], D('49500.00'))

    def test_bank_reconciliation_page_clears_transactions(self):
        bank = PrintTreasury.objects.create(name='CIB', kind='bank', balance=D('1000'))
        PrintTransaction.objects.create(treasury=bank, transaction_type='in', amount=D('500'), category='capital')
        out = PrintTransaction.objects.create(treasury=bank, transaction_type='out', amount=D('200'), category='rent')
        url = f'/printing/accounting/reconcile/{bank.pk}/'
        r = self.get(url)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.context['uncleared']), 2)
        ids = [str(t.pk) for t in r.context['uncleared']]
        r = self.post(url, {'statement_balance': '1300', 'txn': ids, 'statement_date': self.today.isoformat()})
        self.assertEqual(r.status_code, 302)
        rec = bank.reconciliations.get()
        self.assertEqual(rec.cleared_balance, D('1300.00'))
        self.assertEqual(rec.difference, D('0'))
        out.refresh_from_db()
        self.assertTrue(out.cleared)

    # ── 6. ضريبة القيمة المضافة ─────────────────────────────────────────
    def test_vat_return_settles_output_and_input_then_payment_clears_it(self):
        self._delivered_order(price='1000', tax='140')
        self._received_purchase(qty='100', cost='3', tax='42')
        first = self.today.replace(day=1).isoformat()
        r = self.get(f'/printing/accounting/?tab=vat&from={first}&to={self.today.isoformat()}')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context['vat']['output'], D('140.00'))
        self.assertEqual(r.context['vat']['input'], D('42.00'))
        r = self.post('/printing/accounting/vat/file/', {'from': first, 'to': self.today.isoformat()})
        self.assertEqual(r.status_code, 302)
        vat = PrintVatReturn.objects.get()
        self.assertEqual((vat.output_vat, vat.input_vat, vat.net_payable), (D('140.00'), D('42.00'), D('98.00')))
        self.assertEqual(bal(VAT_OUT), D('0'))
        self.assertEqual(bal(VAT_IN), D('0'))
        self.assertEqual(bal(VAT_DUE), D('-98.00'))
        PrintTransaction.objects.create(treasury=self.treasury, transaction_type='out', amount=D('98'),
                                        category='vat', description='سداد إقرار الشهر')
        self.assertEqual(bal(VAT_DUE), D('0'))
        self.post('/printing/accounting/vat/file/', {'from': first, 'to': self.today.isoformat()})
        self.assertEqual(PrintVatReturn.objects.count(), 1)      # فترة متداخلة مرفوضة

    # ── 7. أعمار الديون ──────────────────────────────────────────────────
    def test_aging_buckets_receivables_and_payables(self):
        old = self._delivered_order(price='1000')
        PrintOrder.objects.filter(pk=old.pk).update(date_delivered=timezone.now() - timedelta(days=45))
        self._delivered_order(price='400')
        PrintTransaction.objects.create(treasury=self.treasury, transaction_type='in', amount=D('300'),
                                        customer=self.customer)       # رصيد مقدّم بيقفل من الأقدم
        ar = receivables_aging(self.today)
        row = ar['rows'][0]
        self.assertEqual(row['buckets']['d31_60'], D('700.00'))
        self.assertEqual(row['buckets']['current'], D('400.00'))
        self.assertEqual(ar['total'], D('1100.00'))

        PrintExpenseBill.objects.create(supplier=self.landlord, category='rent', description='إيجار قديم',
                                        amount=D('2000'), date=self.today - timedelta(days=120),
                                        due_date=self.today - timedelta(days=100))
        self._received_purchase(qty='10', cost='5')
        ap = payables_aging(self.today)
        by_name = {r['party'].name: r for r in ap['rows']}
        self.assertEqual(by_name['صاحب المحل']['buckets']['over_90'], D('2000.00'))
        self.assertEqual(by_name['مورد الورق']['buckets']['current'], D('50.00'))
        r = self.get('/printing/accounting/?tab=aging')
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'صاحب المحل')

    # ── 8. إقفال الفترات وإعادة فتحها ─────────────────────────────────
    def test_close_period_locks_it_and_later_changes_post_adjustments(self):
        year = self.today.year - 1
        r = self.post('/printing/accounting/periods/action/', {'action': 'create_year', 'year': str(year)})
        self.assertEqual(r.status_code, 302)
        fy = FiscalYear.objects.get(code=str(year))
        self.assertEqual(fy.periods.count(), 12)
        jan, feb = fy.periods.get(start_date=date(year, 1, 1)), fy.periods.get(start_date=date(year, 2, 1))

        bill = PrintExpenseBill.objects.create(supplier=self.landlord, category='rent', description='إيجار يناير',
                                               amount=D('3000'), date=date(year, 1, 15))
        self.post('/printing/accounting/periods/action/', {'action': 'close', 'period': feb.pk})
        feb.refresh_from_db()
        self.assertFalse(feb.is_closed)                            # بالترتيب: يناير الأول
        self.post('/printing/accounting/periods/action/', {'action': 'close', 'period': jan.pk})
        jan.refresh_from_db()
        self.assertTrue(jan.is_closed)
        self.assertTrue(JournalEntry.objects.filter(reference=f'CLOSE-{jan.pk}').exists())
        self.assertEqual(bal(RENT), D('0'))                         # المصروف اتقفل في الأرباح المحتجزة
        self.assertEqual(bal('3100'), D('3000.00'))

        bill.cancel()                                               # تعديل مستند من شهر مقفول
        entries = JournalEntry.objects.filter(reference=f'PRN-BILL-{bill.pk}').order_by('pk')
        self.assertEqual(entries.count(), 2)
        self.assertEqual(entries.last().date, self.today)          # تسوية بتاريخ النهارده
        self.assertEqual(entries.first().date, date(year, 1, 15))  # يناير ماتلمسش

        # الشهر الحالي مايتقفلش، وإعادة الفتح بتشيل قيود الإقفال
        current = AccountingPeriod.objects.filter(start_date__lte=self.today, end_date__gte=self.today).first()
        if current is not None:
            self.post('/printing/accounting/periods/action/', {'action': 'close', 'period': current.pk})
            current.refresh_from_db()
            self.assertFalse(current.is_closed)
        self.post('/printing/accounting/periods/action/', {'action': 'reopen', 'period': jan.pk})
        jan.refresh_from_db()
        self.assertFalse(jan.is_closed)
        self.assertFalse(JournalEntry.objects.filter(reference__in=[f'CLOSE-{jan.pk}', f'CLOSE-RE-{jan.pk}']).exists())
        r = self.get('/printing/accounting/?tab=periods')
        self.assertEqual(r.status_code, 200)

    # ── 9. استحقاق المرتبات ──────────────────────────────────────────────
    def test_payroll_is_accrued_in_its_month_and_cleared_on_payment(self):
        from hr.models import Employee, HRSettings
        from hr.services.payroll_service import PayrollService
        HRSettings.objects.get_or_create(pk=1)
        worker = User.objects.create_user('print_worker', password='x', first_name='محمود')
        Employee.objects.create(user=worker, department='printing', base_salary=D('6000.00'))
        prev = self._months_ago(1)
        run = PayrollService.generate_payroll(prev.month, prev.year, created_by=self.user)
        PayrollService.approve_payroll(run.pk, approved_by=self.user)
        je = JournalEntry.objects.get(reference=f'PRN-PAYROLL-{run.pk}')
        self.assertEqual(je.date, ledger._month_end(prev.year, prev.month))
        self.assertEqual(bal(SALARIES), D('6000.00'))
        self.assertEqual(bal(SAL_PAYABLE), D('-6000.00'))
        PayrollService.disburse_payroll(run.pk)
        self.assertEqual(PrintTransaction.objects.get(description__startswith='راتب').category, 'payroll')
        self.assertEqual(bal(SAL_PAYABLE), D('0'))
        self.assertEqual(bal(SALARIES), D('6000.00'))              # مش مرتين
        ledger.bootstrap(force=True)
        self.assertEqual(bal(SALARIES), D('6000.00'))

    # ── 10. تصليحات ─────────────────────────────────────────────────────
    def test_ink_drawn_from_stock_is_not_costed_twice(self):
        machine = MachineProfile.objects.create(name='Konica', ink_black_cost_per_ml=D('2'))
        order = PrintOrder.objects.create(customer=self.customer, status='confirmed')
        job = PrintJob.objects.create(order=order, description='فلاير', quantity=1, unit_price=D('100'),
                                      machine=machine, ink_black_ml=D('10'), proof_waived=True)
        self.assertEqual(job.ink_cost, D('20'))
        ink = PrintMaterial.objects.create(name='حبر أسود', category='ink', unit='لتر', quantity=D('5'),
                                           cost_per_unit=D('300'))
        PrintJobMaterial.objects.create(job=job, material=ink, quantity=D('0.1'))
        self.assertEqual(job.ink_cost, D('0'))

    def test_old_ledger_is_upgraded_to_per_treasury_accounts(self):
        from inventory.services.accounting_service import AccountingService
        ref = ledger.ref_treasury_opening(self.treasury.pk)
        JournalEntry.objects.filter(reference=ref).delete()
        AccountingService.post_journal(description='افتتاحي قديم', reference=ref, journal_type='opening', lines=[
            {'account': ledger.account('cash'), 'debit': D('50000'), 'credit': 0},
            {'account': ledger.account('opening_equity'), 'debit': 0, 'credit': D('50000')}])
        PrintLedgerState.objects.update_or_create(pk=1, defaults={'bootstrapped_at': timezone.now(), 'version': 1})
        self.assertEqual(ledger.account('cash').balance, D('50000.00'))
        ledger.ensure_bootstrapped()
        self.assertEqual(PrintLedgerState.get().version, ledger.LEDGER_VERSION)
        self.assertEqual(ledger.account('cash').balance, D('0'))
        self.assertEqual(ledger.treasury_account(self.treasury).balance, D('50000.00'))

    # ── 11. الشاشات ─────────────────────────────────────────────────────
    def test_new_admin_pages_and_tabs_open(self):
        order = self._delivered_order(price='500', paid='500')
        cn = PrintCreditNote.objects.create(order=order, reason='خصم', amount=D('50'))
        asset = PrintFixedAsset.objects.create(name='كمبيوتر', cost=D('9000'), funding='credit', supplier=self.supplier)
        bill = PrintExpenseBill.objects.create(supplier=self.landlord, description='صيانة', category='maintenance',
                                               amount=D('250'))
        p = self._received_purchase(qty='5', cost='2')
        ret = MaterialPurchaseReturn.objects.create(purchase=p)
        bank = PrintTreasury.objects.create(name='بنك', kind='bank')
        transfer = PrintTreasuryTransfer.objects.create(from_treasury=self.treasury, to_treasury=bank, amount=D('10'))
        base = '/secure-portal/printing'
        urls = [
            f'{base}/printcreditnote/', f'{base}/printcreditnote/add/?order={order.pk}',
            f'{base}/printcreditnote/{cn.pk}/change/',
            f'{base}/printfixedasset/', f'{base}/printfixedasset/add/', f'{base}/printfixedasset/{asset.pk}/change/',
            f'{base}/printexpensebill/', f'{base}/printexpensebill/add/', f'{base}/printexpensebill/{bill.pk}/change/',
            f'{base}/materialpurchasereturn/', f'{base}/materialpurchasereturn/add/?purchase={p.pk}',
            f'{base}/materialpurchasereturn/{ret.pk}/change/',
            f'{base}/printtreasurytransfer/', f'{base}/printtreasurytransfer/add/',
            f'{base}/printtreasurytransfer/{transfer.pk}/change/',
            f'{base}/printbankreconciliation/', f'{base}/printvatreturn/',
            f'{base}/printtreasury/', f'{base}/materialpurchase/{p.pk}/change/',
            f'{base}/printorder/{order.pk}/change/', f'{base}/printsupplier/{self.supplier.pk}/change/',
            '/printing/accounting/', '/printing/accounting/?tab=sheet', '/printing/accounting/?tab=journal',
            '/printing/accounting/?tab=aging', '/printing/accounting/?tab=vat', '/printing/accounting/?tab=periods',
            f'/printing/accounting/reconcile/{bank.pk}/',
            f'/printing/customer/{self.customer.pk}/statement/', f'/printing/supplier/{self.supplier.pk}/statement/',
            f'/printing/supplier/{self.landlord.pk}/statement/', '/printing/reports/profit-loss/',
        ]
        for url in urls:
            self.assertEqual(self.get(url).status_code, 200, url)

    def test_admin_creates_credit_note_bill_asset_transfer_and_return(self):
        order = self._delivered_order(price='700', paid='700')
        base = '/secure-portal/printing'
        r = self.post(f'{base}/printcreditnote/add/', {
            'kind': 'return', 'order': order.pk, 'date': self.today.isoformat(), 'reason': 'مرتجع', 'amount': '100',
            'tax_amount': '0', 'refund_treasury': self.treasury.pk})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(PrintCreditNote.objects.get().created_by, self.user)
        r = self.post(f'{base}/printcreditnote/add/', {
            'kind': 'return', 'order': order.pk, 'date': self.today.isoformat(), 'reason': 'زيادة', 'amount': '1000', 'tax_amount': '0'})
        self.assertEqual(r.status_code, 200)                       # أكبر من الطلب: خطأ في الفورم
        self.assertEqual(PrintCreditNote.objects.count(), 1)

        r = self.post(f'{base}/printexpensebill/add/', {
            'supplier': self.landlord.pk, 'date': self.today.isoformat(), 'category': 'rent',
            'description': 'إيجار', 'amount': '2500', 'tax_amount': '0', 'payment_terms': 'credit'})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(bal(RENT), D('2500.00'))

        r = self.post(f'{base}/printfixedasset/add/', {
            'name': 'ماكينة لامينيشن', 'category': 'equipment', 'purchase_date': self.today.isoformat(),
            'funding': 'cash', 'treasury': self.treasury.pk, 'cost': '4000', 'salvage_value': '0',
            'useful_life_months': '48', 'opening_accumulated': '0', 'disposal_amount': '0'})
        self.assertEqual(r.status_code, 302, getattr(r, 'context', None) and r.context['adminform'].form.errors)
        self.assertEqual(bal(FIXED), D('4000.00'))

        bank = PrintTreasury.objects.create(name='بنك الإسكندرية', kind='bank')
        r = self.post(f'{base}/printtreasurytransfer/add/', {
            'from_treasury': self.treasury.pk, 'to_treasury': bank.pk, 'amount': '1500',
            'date_0': self.today.isoformat(), 'date_1': '10:00:00', 'notes': 'إيداع البنك'})
        self.assertEqual(r.status_code, 302)
        bank.refresh_from_db()
        self.assertEqual(bank.balance, D('1500.00'))

        p = self._received_purchase(qty='20', cost='3')
        r = self.post(f'{base}/materialpurchasereturn/add/', {
            'purchase': p.pk, 'date': self.today.isoformat(), 'reason': 'تالف'})
        self.assertEqual(r.status_code, 302)
        ret = MaterialPurchaseReturn.objects.get()
        r = self.post(f'{base}/materialpurchasereturn/{ret.pk}/change/', {
            'date': self.today.isoformat(), 'reason': 'تالف', 'post_now': 'on',
            'lines-TOTAL_FORMS': '1', 'lines-INITIAL_FORMS': '0', 'lines-MIN_NUM_FORMS': '0',
            'lines-MAX_NUM_FORMS': '1000', 'lines-0-purchase_line': p.lines.get().pk, 'lines-0-quantity': '5'})
        self.assertEqual(r.status_code, 302)
        ret.refresh_from_db()
        self.assertEqual(ret.status, 'posted')
        self.assertEqual(ret.value, D('15.00'))
