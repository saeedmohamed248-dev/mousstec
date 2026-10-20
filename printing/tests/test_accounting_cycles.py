"""
📒 دورات المطبعة المحاسبية الكاملة — مشتريات، إنتاج، استحقاق، رصيد مقدّم.

* شراء خامات من مورد: المخزون يزيد بمتوسط تكلفة والفلوس تطلع من الخزنة لوحدها
  (نقدي) أو تفضل على حساب المورد (آجل) — والقيد: مخزون + ض.ق.م مدخلات / موردين.
* مراحل الإنتاج: تصميم ← بروفة ← طباعة ← تشطيب، والطباعة مستنية موافقة العميل.
* الإيراد بيتعرف وقت التسليم (استحقاق)، والعربون قبل التسليم التزام مش إيراد.
* رصيد مقدّم على حساب العميل نفسه + تسديد طلب منه + حد ائتمان.
* كل قيد متوازن، وميزان المراجعة بيتزن بعد كل سيناريو.
"""
import json
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.db.models import Sum
from django.test import Client, override_settings
from django.utils import timezone

from inventory.models import AccountingEntry, AccountingPeriod, FiscalYear, JournalEntry
from inventory.services.accounting_reports import AccountingReportService
from inventory.tests.base import ERPTenantTestCase
from printing import ledger
from printing.models import (
    MaterialPurchase, MaterialPurchaseLine, PriceQuotation, PrintCreditAllocation, PrintCustomer,
    PrintJob, PrintJobMaterial, PrintLedgerState, PrintMaterial, PrintOrder, PrintProof,
    PrintStockAdjustment, PrintSupplier, PrintTransaction, PrintTreasury, QuotationLine,
)
from printing.views.finance import convert_quotation

User = get_user_model()
D = Decimal


def bal(code):
    """رصيد الحساب في الدفتر (مدين − دائن)."""
    agg = AccountingEntry.objects.filter(account__code=code).aggregate(d=Sum('debit'), c=Sum('credit'))
    return (agg['d'] or D('0')) - (agg['c'] or D('0'))


CASH, AR, INVENTORY, WIP, VAT_IN, SUP_ADV = '1001', '1100', '1200', '1220', '1250', '1260'
AP, VAT_OUT, DEPOSITS, OPENING, CAPITAL = '2100', '2200', '2400', '3005', '3001'
REVENUE, OTHER_INCOME, COGS, VARIANCE, SPOILAGE, RENT = '4010', '4099', '5001', '5160', '5170', '5201'


@override_settings(ALLOWED_HOSTS=['*'])
class PrintingAccountingCycleTests(ERPTenantTestCase):

    def setUp(self):
        super().setUp()
        connection.set_tenant(self.tenant)
        self.host = self.domain.domain
        self.treasury = PrintTreasury.objects.create(name='الخزنة الرئيسية', balance=D('10000'))
        self.customer = PrintCustomer.objects.create(name='شركة النور', phone='01011112222')
        self.supplier = PrintSupplier.objects.create(name='مورد الورق', phone='01099998888')
        self.paper = PrintMaterial.objects.create(name='ورق كوشيه', unit='فرخ', quantity=D('10'),
                                                  cost_per_unit=D('2'))
        self.user = User.objects.create_superuser('boss', 'b@x.com', 'x')

    def tearDown(self):
        self.assert_books_balance()
        super().tearDown()

    # ── helpers ────────────────────────────────────────────────────────
    def assert_books_balance(self):
        tb = AccountingReportService.trial_balance()
        self.assertTrue(tb['is_balanced'], tb)
        # الدفتر = الحقيقة: النقدية = أرصدة الخزائن، والمخزون = قيمة الخامات
        # (فرق قروش بس من تقريب متوسط التكلفة).
        self.assertEqual(bal(CASH), sum(t.balance for t in PrintTreasury.objects.all()))
        stock = sum((m.quantity * m.cost_per_unit for m in PrintMaterial.objects.all()), D('0'))
        self.assertLess(abs(bal(INVENTORY) - stock), D('1.00'))

    def _purchase(self, qty='100', cost='3', tax='0', terms='cash', **kw):
        p = MaterialPurchase.objects.create(supplier=self.supplier, payment_terms=terms,
                                            treasury=self.treasury if terms == 'cash' else None,
                                            tax_amount=D(tax), created_by=self.user, **kw)
        MaterialPurchaseLine.objects.create(purchase=p, material=self.paper, quantity=D(qty), unit_cost=D(cost))
        p.refresh_from_db()
        return p

    def _order(self, price='1000', tax='0', status='confirmed', **kw):
        order = PrintOrder.objects.create(customer=self.customer, status=status, tax_amount=D(tax), **kw)
        job = PrintJob.objects.create(order=order, description='كروت شخصية', quantity=1,
                                      unit_price=D(price), proof_waived=True)
        order.refresh_from_db()
        return order, job

    def _pay(self, amount, order=None, customer=None, kind='in', **kw):
        return PrintTransaction.objects.create(treasury=self.treasury, transaction_type=kind, amount=D(amount),
                                               order=order, customer=customer, created_by=self.user, **kw)

    # ── 1. شراء الخامات ────────────────────────────────────────────────
    def test_cash_purchase_receives_stock_pays_treasury_and_posts(self):
        self.assertEqual(bal(INVENTORY), D('20.00'))           # مخزون افتتاحي 10 × 2
        self.assertEqual(bal(OPENING), D('-10020.00'))
        p = self._purchase(qty='100', cost='3', tax='42')
        self.assertEqual((p.total_amount, p.net_total), (D('300.00'), D('342.00')))
        # المسودة مابتحركش حاجة
        self.paper.refresh_from_db()
        self.assertEqual(self.paper.quantity, D('10'))
        self.assertFalse(JournalEntry.objects.filter(reference=ledger.ref_purchase(p.pk)).exists())

        p.receive(user=self.user)
        self.paper.refresh_from_db()
        self.treasury.refresh_from_db()
        self.assertEqual(self.paper.quantity, D('110'))
        self.assertEqual(self.paper.cost_per_unit, D('2.9091'))  # (20 + 300) / 110 — متوسط مرجّح
        self.assertEqual(self.treasury.balance, D('9658.00'))  # الفلوس طلعت لوحدها
        self.assertEqual((p.status, p.paid_amount, p.remaining), ('received', D('342.00'), D('0.00')))
        self.assertEqual(bal(INVENTORY), D('320.00'))
        self.assertEqual(bal(VAT_IN), D('42.00'))
        self.assertEqual(bal(AP), D('0.00'))
        self.assertEqual(self.supplier.balance, D('0.00'))
        txn = p.payments.get()
        self.assertEqual((txn.category, txn.supplier_id), ('supplier', self.supplier.pk))
        with self.assertRaises(ValidationError):
            p.receive()                                         # مرة واحدة بس

    def test_credit_purchase_partial_payment_and_supplier_advance(self):
        p = self._purchase(qty='50', cost='4', terms='credit')
        p.receive(user=self.user)
        self.treasury.refresh_from_db()
        self.assertEqual(self.treasury.balance, D('10000'))     # آجل: الخزنة ماتلمستش
        self.assertEqual(bal(AP), D('-200.00'))
        self.assertEqual(self.supplier.balance, D('200.00'))

        PrintTransaction.objects.create(treasury=self.treasury, transaction_type='out', amount=D('150'),
                                        purchase=p)
        p.refresh_from_db()
        self.assertEqual((p.paid_amount, p.remaining), (D('150.00'), D('50.00')))
        self.assertEqual(self.supplier.balance, D('50.00'))
        self.assertEqual(bal(AP), D('-50.00'))

        # دفعة على حساب المورد أكبر من المستحق ← دفعة مقدمة للمورد (أصل)
        PrintTransaction.objects.create(treasury=self.treasury, transaction_type='out', amount=D('80'),
                                        supplier=self.supplier)
        self.assertEqual(self.supplier.balance, D('-30.00'))
        self.assertEqual(bal(AP), D('0.00'))
        self.assertEqual(bal(SUP_ADV), D('30.00'))

    def test_receive_refused_when_treasury_short(self):
        poor = PrintTreasury.objects.create(name='درج صغير', balance=D('50'))
        p = MaterialPurchase.objects.create(supplier=self.supplier, treasury=poor)
        MaterialPurchaseLine.objects.create(purchase=p, material=self.paper, quantity=D('10'), unit_cost=D('10'))
        with self.assertRaises(ValidationError):
            p.receive()
        p.refresh_from_db()
        self.paper.refresh_from_db()
        poor.refresh_from_db()
        self.assertEqual((p.status, self.paper.quantity, poor.balance), ('draft', D('10'), D('50')))
        self.assertFalse(JournalEntry.objects.filter(reference=ledger.ref_purchase(p.pk)).exists())

    def test_cancel_received_purchase_returns_stock_and_keeps_payment_as_credit(self):
        p = self._purchase(qty='100', cost='3')
        p.receive()
        p.cancel()
        self.paper.refresh_from_db()
        self.assertEqual(self.paper.quantity, D('10'))
        self.assertLess(abs(self.paper.cost_per_unit - D('2')), D('0.001'))
        self.assertEqual(bal(INVENTORY), D('20.00'))
        self.assertEqual(self.supplier.balance, D('-300.00'))   # دفعناله ورجّعنا البضاعة
        self.assertEqual(bal(SUP_ADV), D('300.00'))
        # المورد رجّع الفلوس ← الرصيد يتقفل
        PrintTransaction.objects.create(treasury=self.treasury, transaction_type='in', amount=D('300'),
                                        supplier=self.supplier)
        self.assertEqual(self.supplier.balance, D('0.00'))
        self.assertEqual(bal(SUP_ADV), D('0.00'))
        with self.assertRaises(ValidationError):                 # الملغية مابتتعدّلش
            MaterialPurchaseLine.objects.create(purchase=p, material=self.paper, quantity=D('1'),
                                                unit_cost=D('1'))

    def test_cancel_refused_when_stock_already_consumed(self):
        p = self._purchase(qty='100', cost='3')
        p.receive()
        order, job = self._order()
        PrintJobMaterial.objects.create(job=job, material=self.paper, quantity=D('105'))
        with self.assertRaises(ValidationError):
            p.cancel()
        p.refresh_from_db()
        self.assertEqual(p.status, 'received')

    def test_received_purchase_cannot_be_deleted(self):
        p = self._purchase(terms='credit')
        p.receive()
        with self.assertRaises(ValidationError):
            p.delete()

    # ── 2. الإيراد بالاستحقاق ──────────────────────────────────────────
    def test_revenue_recognised_on_delivery_not_on_payment(self):
        order, job = self._order(price='1000', tax='140')
        self._pay('500', order=order)                            # عربون قبل التسليم
        self.assertEqual(bal(REVENUE), D('0.00'))
        self.assertEqual(bal(DEPOSITS), D('-500.00'))           # التزام: فلوس العميل عندنا
        self.assertEqual(bal(AR), D('0.00'))

        PrintJobMaterial.objects.create(job=job, material=self.paper, quantity=D('5'))   # 5 × 2
        self.assertEqual(bal(WIP), D('10.00'))
        self.assertEqual(bal(INVENTORY), D('10.00'))

        order.status = 'delivered'
        order.save()
        self.assertEqual(bal(REVENUE), D('-1000.00'))
        self.assertEqual(bal(VAT_OUT), D('-140.00'))
        self.assertEqual(bal(AR), D('640.00'))                  # 1140 − 500 عربون
        self.assertEqual(bal(DEPOSITS), D('0.00'))
        self.assertEqual(bal(COGS), D('10.00'))
        self.assertEqual(bal(WIP), D('0.00'))
        sales = JournalEntry.objects.get(reference=ledger.ref_order(order.pk))
        self.assertEqual(sales.journal_type, 'sales')
        self.assertEqual(sales.date, timezone.localtime(order.date_delivered).date())

        today = timezone.localdate()
        pnl = AccountingReportService.income_statement(today.replace(day=1), today)
        self.assertEqual(pnl['net_profit'], D('990.00'))         # 1000 − 10 خامات

        self._pay('640', order=order)
        self.assertEqual(bal(AR), D('0.00'))
        order.refresh_from_db()
        self.assertEqual(order.remaining, D('0.00'))

    def test_saving_stale_order_keeps_total_and_paid(self):
        """🐛 نسخة قديمة من الطلب (اتحمّلت قبل المهمة والدفعة) كانت بتصفّر الإجمالي والمدفوع."""
        order = PrintOrder.objects.create(customer=self.customer, status='confirmed')
        PrintJob.objects.create(order=order, description='بنر', quantity=1, unit_price=D('700'), proof_waived=True)
        self._pay('200', order=order)
        order.status = 'delivered'                               # في الذاكرة: إجمالي 0 ومدفوع 0
        order.save()
        order.refresh_from_db()
        self.assertEqual((order.total_amount, order.paid_amount), (D('700'), D('200')))
        self.assertEqual(bal(REVENUE), D('-700.00'))
        self.assertEqual(bal(AR), D('500.00'))

    def test_editing_delivered_order_resyncs_and_undelivering_reverses(self):
        order, job = self._order(price='300')
        order.status = 'delivered'
        order.save()
        job.unit_price = D('400')
        job.save()
        self.assertEqual(bal(REVENUE), D('-400.00'))
        self.assertEqual(JournalEntry.objects.filter(reference=ledger.ref_order(order.pk)).count(), 1)
        order.status = 'ready'
        order.save()
        self.assertEqual(bal(REVENUE), D('0.00'))
        self.assertEqual(bal(AR), D('0.00'))

    def test_cancelled_order_materials_become_spoilage_and_payment_becomes_credit(self):
        order, job = self._order(price='500')
        self._pay('200', order=order)
        PrintJobMaterial.objects.create(job=job, material=self.paper, quantity=D('3'))
        order.status = 'cancelled'
        order.save()
        self.assertEqual(bal(SPOILAGE), D('6.00'))
        self.assertEqual(bal(WIP), D('0.00'))
        summary = self.customer.account_summary()
        self.assertEqual((summary['balance'], summary['credit']), (D('-200.00'), D('200.00')))
        self.assertEqual(bal(DEPOSITS), D('-200.00'))

    def test_deleting_order_unposts_its_entries(self):
        order, job = self._order(price='500')
        PrintJobMaterial.objects.create(job=job, material=self.paper, quantity=D('3'))
        order.status = 'delivered'
        order.save()
        order.delete()
        self.assertFalse(JournalEntry.objects.filter(reference=ledger.ref_order(order.pk)).exists())
        self.assertEqual(bal(REVENUE), D('0.00'))
        self.paper.refresh_from_db()
        self.assertEqual(self.paper.quantity, D('10'))

    # ── 3. الرصيد المقدّم وحد الائتمان ─────────────────────────────────
    def test_on_account_advance_then_settle_orders_from_it(self):
        self._pay('1000', customer=self.customer)                # رصيد مقدّم من غير طلب
        s = self.customer.account_summary()
        self.assertEqual((s['credit'], s['balance']), (D('1000.00'), D('-1000.00')))
        self.assertEqual(bal(DEPOSITS), D('-1000.00'))

        order, _job = self._order(price='600')
        self.treasury.refresh_from_db()
        before = self.treasury.balance
        applied = PrintCreditAllocation.apply_available(order, user=self.user)
        self.assertEqual(applied, D('600.00'))
        order.refresh_from_db()
        self.assertEqual((order.paid_amount, order.remaining), (D('600.00'), D('0.00')))
        self.treasury.refresh_from_db()
        self.assertEqual(self.treasury.balance, before)          # التسديد مش حركة خزنة
        self.assertEqual(self.customer.account_summary()['credit'], D('400.00'))

        other, _ = self._order(price='900')
        with self.assertRaises(ValidationError):                 # أكبر من الرصيد المتاح
            PrintCreditAllocation.objects.create(order=other, amount=D('500'))
        PrintCreditAllocation.objects.create(order=other, amount=D('400'))
        other.refresh_from_db()
        self.assertEqual(other.remaining, D('500.00'))
        self.assertEqual(self.customer.account_summary()['credit'], D('0.00'))

        # إلغاء طلب متسدّد من الرصيد ← الرصيد يرجع متاح
        order.status = 'cancelled'
        order.save()
        self.assertEqual(self.customer.account_summary()['credit'], D('600.00'))

    def test_overpaid_order_excess_is_customer_credit(self):
        order, _ = self._order(price='100')
        self._pay('150', order=order)
        self.assertEqual(self.customer.account_summary()['credit'], D('50.00'))

    def test_credit_limit_keeps_confirmed_order_as_draft(self):
        self.customer.credit_limit = D('500')
        self.customer.save()
        order, _ = self._order(price='800', status='draft')
        order.status = 'confirmed'
        order.save()
        msg = order.apply_credit_hold(previous_status='draft')
        self.assertIn('حد الائتمان', msg)
        order.refresh_from_db()
        self.assertEqual(order.status, 'draft')

        self._pay('400', customer=self.customer)                 # دفع مقدّم يقلّل المديونية
        order.status = 'confirmed'
        order.save()
        self.assertIsNone(order.apply_credit_hold(previous_status='draft'))
        order.refresh_from_db()
        self.assertEqual(order.status, 'confirmed')

    def test_quote_conversion_respects_credit_limit(self):
        self.customer.credit_limit = D('100')
        self.customer.save()
        q = PriceQuotation.objects.create(title='بنرات', customer=self.customer, status='accepted')
        QuotationLine.objects.create(quotation=q, description='بنر', quantity=D('1'), unit_price=D('300'))
        order = convert_quotation(q)
        order.refresh_from_db()
        self.assertEqual(order.status, 'draft')
        self.assertTrue(getattr(order, 'credit_hold_message', ''))

    # ── 4. مراحل الإنتاج والبروفة ──────────────────────────────────────
    def _proof_file(self):
        return SimpleUploadedFile('proof.png', b'\x89PNG\r\n\x1a\nfake', content_type='image/png')

    def test_printing_waits_for_customer_proof_approval(self):
        order = PrintOrder.objects.create(customer=self.customer, status='confirmed')
        job = PrintJob.objects.create(order=order, description='فلاير', quantity=100, unit_price=D('2'))
        self.assertEqual(job.stage, 'design')
        job.stage = 'printing'
        with self.assertRaises(ValidationError):
            job.full_clean()
        job.refresh_from_db()
        self.assertEqual(job.advance_stage(), 'proof')
        with self.assertRaises(ValidationError):
            job.advance_stage()                                  # الطباعة محتاجة بروفة موافق عليها
        job.refresh_from_db()
        self.assertEqual(job.stage, 'proof')

        proof = PrintProof.objects.create(job=job, file=self._proof_file(), created_by=self.user)
        job.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual((job.stage, order.status, proof.version), ('proof', 'in_progress', 1))

        c = Client()
        r = c.get(f'/printing/proof/{proof.share_token}/', HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 200)
        r = c.post(f'/printing/proof/{proof.share_token}/respond/',
                   json.dumps({'action': 'reject', 'comment': 'كبّر اللوجو'}),
                   content_type='application/json', HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 200, r.content)
        job.refresh_from_db()
        proof.refresh_from_db()
        self.assertEqual((job.stage, proof.status, proof.customer_comment), ('design', 'changes_requested', 'كبّر اللوجو'))

        v2 = PrintProof.objects.create(job=job, file=self._proof_file())
        self.assertEqual(v2.version, 2)
        with self.assertRaises(ValidationError):
            proof.respond('approve')                             # نسخة قديمة
        r = c.post(f'/printing/proof/{v2.share_token}/respond/', json.dumps({'action': 'approve'}),
                   content_type='application/json', HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 200, r.content)
        job.refresh_from_db()
        self.assertEqual(job.stage, 'printing')
        self.assertEqual(job.advance_stage(), 'finishing')
        self.assertEqual(job.advance_stage(), 'done')
        job.refresh_from_db()
        order.refresh_from_db()
        self.assertTrue(job.is_complete)
        self.assertEqual(order.status, 'ready')

    def test_waived_proof_goes_straight_to_printing(self):
        order, job = self._order()
        self.assertEqual(job.advance_stage(), 'printing')        # من غير بروفة
        job.is_complete = True
        job.save()
        job.refresh_from_db()
        self.assertEqual(job.stage, 'done')

    # ── 5. تسويات المخزن والمصروفات ─────────────────────────────────────
    def test_stock_adjustment_posts_variance_and_delete_restores(self):
        adj = PrintStockAdjustment.objects.create(material=self.paper, quantity_change=D('-4'), reason='damage')
        self.paper.refresh_from_db()
        self.assertEqual(self.paper.quantity, D('6'))
        self.assertEqual(bal(VARIANCE), D('8.00'))
        self.assertEqual(bal(INVENTORY), D('12.00'))
        with self.assertRaises(ValidationError):
            PrintStockAdjustment.objects.create(material=self.paper, quantity_change=D('-100'))
        adj.delete()
        self.paper.refresh_from_db()
        self.assertEqual(self.paper.quantity, D('10'))
        self.assertEqual(bal(VARIANCE), D('0.00'))

    def test_expenses_capital_and_transfers_land_on_the_right_accounts(self):
        PrintTransaction.objects.create(treasury=self.treasury, transaction_type='out', amount=D('2000'),
                                        description='إيجار شهر أكتوبر')
        PrintTransaction.objects.create(treasury=self.treasury, transaction_type='in', amount=D('5000'),
                                        category='capital', description='المالك حط فلوس')
        branch = PrintTreasury.objects.create(name='خزنة الفرع')
        PrintTransaction.objects.create(treasury=self.treasury, transaction_type='out', amount=D('700'),
                                        category='transfer', description='تحويل للفرع')
        PrintTransaction.objects.create(treasury=branch, transaction_type='in', amount=D('700'),
                                        category='transfer', description='تحويل من الرئيسية')
        self.assertEqual(bal(RENT), D('2000.00'))
        self.assertEqual(bal(CAPITAL), D('-5000.00'))
        self.assertEqual(bal('1090'), D('0.00'))
        today = timezone.localdate()
        pnl = AccountingReportService.income_statement(today.replace(day=1), today)
        self.assertEqual(pnl['net_profit'], D('-2000.00'))      # رأس المال والتحويل مش إيراد

    def test_editing_payment_in_closed_period_posts_adjustment_today(self):
        old_day = timezone.now() - timedelta(days=60)
        order, _ = self._order(price='300')
        txn = self._pay('100', order=order, date=old_day)
        d = timezone.localtime(old_day).date()
        fy = FiscalYear.objects.create(code='FY-T', name='T', start_date=d, end_date=d)
        AccountingPeriod.objects.create(fiscal_year=fy, name='closed', start_date=d, end_date=d,
                                        is_closed=True)
        txn.amount = D('150')
        txn.save()
        jes = JournalEntry.objects.filter(reference=ledger.ref_txn(txn.pk)).order_by('pk')
        self.assertEqual(jes.count(), 2)
        self.assertEqual(jes.last().journal_type, 'adjustment')
        self.assertEqual(jes.last().date, timezone.localdate())
        self.assertEqual(bal(CASH), self.treasury.__class__.objects.get(pk=self.treasury.pk).balance)

    # ── 6. bootstrap للبيانات القديمة ───────────────────────────────────
    def test_bootstrap_rebuilds_ledger_for_legacy_data(self):
        order, job = self._order(price='800', tax='112')
        self._pay('300', order=order)
        PrintJobMaterial.objects.create(job=job, material=self.paper, quantity=D('2'))
        order.status = 'delivered'
        order.save()
        self._pay('50', customer=self.customer)
        PrintTransaction.objects.create(treasury=self.treasury, transaction_type='out', amount=D('120'),
                                        description='فاتورة كهرباء')
        snapshot = {code: bal(code) for code in (CASH, AR, INVENTORY, REVENUE, VAT_OUT, COGS, DEPOSITS)}

        # بيانات قديمة: مفيش ولا قيد
        JournalEntry.objects.all().delete()
        PrintLedgerState.objects.all().delete()
        self.assertTrue(ledger.bootstrap())
        self.assertFalse(ledger.bootstrap())                     # مرة واحدة
        self.assertEqual({code: bal(code) for code in snapshot}, snapshot)
        ledger.bootstrap(force=True)                              # تكرارها مابيكرّرش قيود
        self.assertEqual({code: bal(code) for code in snapshot}, snapshot)
