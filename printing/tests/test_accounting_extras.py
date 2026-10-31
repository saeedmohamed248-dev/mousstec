"""
📒 آخر نواقص الدورة المحاسبية للمطابع.

* رصيد افتتاحي للعملاء والموردين (قبل السيستم) — في الدفتر والكشوف والأعمار.
* الخصم من المنبع (ضريبة الخصم والإضافة) على دفعات العملاء وسداد الموردين + توريده.
* الشيكات: وارد بيسدّد الطلب ويتحصّل في البنك أو يرجع، وصادر بيسدّد المورد ويتصرف.
* الإشعار الدائن: إعدام دين وخصم/فروق كسور (مصروف) غير المرتجع، وعلى حساب العميل نفسه.
* التسديد من رصيدنا المقدّم عند المورد على فواتيره.
* فرع لكل قيد + قائمة دخل لكل فرع، كارت الصنف، وتصدير Excel.
* كل سيناريو: ميزان المراجعة متزن وكل خزنة/حافظة = حسابها في الدفتر.
"""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import connection
from django.db.models import Q, Sum
from django.test import Client, override_settings
from django.utils import timezone

from inventory.models import AccountingEntry, JournalEntry
from inventory.services.accounting_reports import AccountingReportService
from inventory.tests.base import ERPTenantTestCase
from printing import ledger
from printing.models import (
    MaterialPurchase, MaterialPurchaseLine, MaterialPurchaseReturn, MaterialPurchaseReturnLine, PrintBranch,
    PrintCheque, PrintCreditAllocation, PrintCreditNote, PrintCustomer, PrintExpenseBill, PrintFixedAsset,
    PrintJob, PrintJobMaterial, PrintJournalBranch, PrintLedgerState, PrintMaterial, PrintOrder,
    PrintStockAdjustment, PrintSupplier, PrintSupplierAllocation, PrintTransaction, PrintTreasury,
    PrintTreasuryTransfer,
)
from printing.views.accounting import material_movements, payables_aging, receivables_aging

User = get_user_model()
D = Decimal
XLSX = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'


def bal(code):
    """رصيد الحساب في الدفتر (مدين − دائن) — شامل حساباته الفرعية."""
    agg = (AccountingEntry.objects.filter(Q(account__code=code) | Q(account__parent__code=code))
           .aggregate(d=Sum('debit'), c=Sum('credit')))
    return (agg['d'] or D('0')) - (agg['c'] or D('0'))


AR, CHEQ_IN, WHT_IN, AP, CHEQ_OUT, WHT_OUT = '1100', '1120', '1270', '2100', '2130', '2230'
DEPOSITS, SUP_ADV, OPENING, REVENUE, BAD_DEBTS, DISCOUNT = '2400', '1260', '3005', '4010', '5800', '5820'


@override_settings(ALLOWED_HOSTS=['*'])
class PrintingAccountingExtrasTests(ERPTenantTestCase):

    def setUp(self):
        super().setUp()
        connection.set_tenant(self.tenant)
        type(self.tenant).objects.filter(pk=self.tenant.pk).update(industry='printing')
        self.host = self.domain.domain
        self.treasury = PrintTreasury.objects.create(name='الخزنة', balance=D('20000'))
        self.bank = PrintTreasury.objects.create(name='بنك مصر', kind='bank', balance=D('10000'))
        self.customer = PrintCustomer.objects.create(name='شركة الندى', phone='01011112222')
        self.supplier = PrintSupplier.objects.create(name='مورد الحبر', phone='01099998888')
        self.paper = PrintMaterial.objects.create(name='ورق 80 جم', unit='رزمة', quantity=D('10'),
                                                  cost_per_unit=D('100'))
        self.user = User.objects.create_superuser('extras_boss', 'e@x.com', 'x')
        self.c = Client()
        self.c.force_login(self.user)
        self.today = timezone.localdate()

    def tearDown(self):
        self.assert_books_balance()
        super().tearDown()

    def assert_books_balance(self):
        tb = AccountingReportService.trial_balance()
        self.assertTrue(tb['is_balanced'], tb)
        for t in PrintTreasury.objects.all():      # كل خزنة وحافظة = حسابها في الدفتر
            acc = ledger.treasury_account(t)
            agg = acc.entries.aggregate(d=Sum('debit'), c=Sum('credit'))
            self.assertEqual((agg['d'] or D('0')) - (agg['c'] or D('0')), t.balance, t.name)
        stock = sum((m.quantity * m.cost_per_unit for m in PrintMaterial.objects.all()), D('0'))
        self.assertLess(abs(bal('1200') - stock), D('1.00'))

    def get(self, url, **kw):
        return self.c.get(url, HTTP_HOST=self.host, **kw)

    def post(self, url, data):
        return self.c.post(url, data, HTTP_HOST=self.host)

    def _delivered(self, price='1000', customer=None, branch=None):
        order = PrintOrder.objects.create(customer=customer or self.customer, status='confirmed', branch=branch)
        PrintJob.objects.create(order=order, description='بروشور', quantity=1, unit_price=D(price), proof_waived=True)
        order.refresh_from_db()
        order.status = 'delivered'
        order.save()
        order.refresh_from_db()
        return order

    def _purchase(self, qty='10', cost='100', terms='credit'):
        p = MaterialPurchase.objects.create(supplier=self.supplier, payment_terms=terms,
                                            treasury=self.treasury if terms == 'cash' else None)
        MaterialPurchaseLine.objects.create(purchase=p, material=self.paper, quantity=D(qty), unit_cost=D(cost))
        p.refresh_from_db()
        p.receive(user=self.user)
        p.refresh_from_db()
        return p

    # ── 1. الأرصدة الافتتاحية ───────────────────────────────────────────
    def test_customer_opening_debt_is_collected_and_written_off(self):
        self.customer.opening_balance = D('5000')
        self.customer.opening_date = self.today - timedelta(days=120)
        self.customer.save()
        self.assertEqual(bal(AR), D('5000.00'))
        je = JournalEntry.objects.get(reference=ledger.ref_customer_opening(self.customer.pk))
        self.assertEqual((je.date, je.journal_type), (self.customer.opening_date, 'opening'))
        self.assertEqual(self.customer.account_summary()['balance'], D('5000.00'))
        PrintTransaction.objects.create(treasury=self.treasury, transaction_type='in', amount=D('3000'),
                                        customer=self.customer)
        summary = self.customer.account_summary()
        self.assertEqual((summary['balance'], summary['opening_outstanding'], summary['credit']),
                         (D('2000.00'), D('2000.00'), D('0')))
        ar = receivables_aging(self.today)
        self.assertEqual(ar['rows'][0]['buckets']['over_90'], D('2000.00'))
        r = self.get(f'/printing/customer/{self.customer.pk}/statement/')
        self.assertContains(r, 'رصيد افتتاحي')
        self.assertEqual(r.context['final_balance'], D('2000.00'))

        # إعدام الباقي على حساب العميل (من غير طلب) = مصروف ديون معدومة
        PrintCreditNote.objects.create(customer=self.customer, kind='bad_debt', amount=D('2000'),
                                       reason='العميل قفل الشركة')
        self.assertEqual(bal(BAD_DEBTS), D('2000.00'))
        self.assertEqual(bal(AR), D('0.00'))
        self.assertEqual(self.customer.account_summary()['balance'], D('0.00'))
        with self.assertRaises(ValidationError):
            PrintCreditNote.objects.create(customer=self.customer, kind='bad_debt', amount=D('1'), reason='زيادة')

    def test_customer_opening_credit_settles_a_new_order(self):
        self.customer.opening_balance = D('-1000')
        self.customer.save()
        self.assertEqual(bal(DEPOSITS), D('-1000.00'))
        self.assertEqual(self.customer.account_summary()['credit'], D('1000.00'))
        order = self._delivered('1500')
        PrintCreditAllocation.apply_available(order, user=self.user)
        order.refresh_from_db()
        self.assertEqual(order.paid_amount, D('1000.00'))
        self.assertEqual(bal(AR), D('500.00'))
        self.assertEqual(bal(DEPOSITS), D('0.00'))

    def test_supplier_opening_balances(self):
        self.supplier.opening_balance = D('12000')
        self.supplier.opening_date = self.today - timedelta(days=70)
        self.supplier.save()
        self.assertEqual(bal(AP), D('-12000.00'))
        PrintTransaction.objects.create(treasury=self.treasury, transaction_type='out', amount=D('5000'),
                                        supplier=self.supplier)
        self.assertEqual(self.supplier.account_summary()['balance'], D('7000.00'))
        ap = payables_aging(self.today)
        self.assertEqual(ap['rows'][0]['buckets']['d61_90'], D('7000.00'))
        r = self.get(f'/printing/supplier/{self.supplier.pk}/statement/')
        self.assertContains(r, 'رصيد افتتاحي')

        other = PrintSupplier.objects.create(name='مورد قديم', opening_balance=D('-800'))
        self.assertEqual(other.account_summary()['balance'], D('-800.00'))
        self.assertEqual(bal(SUP_ADV), D('800.00'))

    # ── 2. الخصم من المنبع ──────────────────────────────────────────────
    def test_customer_withholding_settles_order_and_follows_its_payment(self):
        order = self._delivered('1000')
        pay = PrintTransaction.objects.create(treasury=self.treasury, transaction_type='in', amount=D('990'),
                                              withholding_tax=D('10'), order=order)
        order.refresh_from_db()
        self.assertEqual((order.paid_amount, order.remaining), (D('1000.00'), D('0.00')))
        wallet = PrintTreasury.objects.get(kind='wht_in')
        self.assertEqual(wallet.balance, D('10.00'))
        self.assertEqual(bal(WHT_IN), D('10.00'))
        self.assertEqual(bal(AR), D('0.00'))
        self.treasury.refresh_from_db()
        self.assertEqual(self.treasury.balance, D('20990.00'))
        with self.assertRaises(ValidationError):
            pay.withholding_txn.delete()                    # تابع للدفعة — مابيتمسحش لوحده

        pay.refresh_from_db()
        pay.amount, pay.withholding_tax = D('980'), D('20')
        pay.save()
        wallet.refresh_from_db()
        self.assertEqual(wallet.balance, D('20.00'))
        pay.delete()
        wallet.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual((wallet.balance, order.paid_amount), (D('0.00'), D('0.00')))

    def test_supplier_withholding_is_a_liability_until_remitted(self):
        p = self._purchase('10', '100')
        PrintTransaction.objects.create(treasury=self.treasury, transaction_type='out', amount=D('990'),
                                        withholding_tax=D('10'), purchase=p)
        p.refresh_from_db()
        self.assertEqual(p.remaining, D('0.00'))
        self.assertEqual(bal(AP), D('0.00'))
        self.assertEqual(bal(WHT_OUT), D('-10.00'))
        wallet = PrintTreasury.objects.get(kind='wht_out')
        self.assertEqual(wallet.balance, D('-10.00'))
        PrintTreasuryTransfer.objects.create(from_treasury=self.bank, to_treasury=wallet, amount=D('10'))
        wallet.refresh_from_db()
        self.assertEqual(wallet.balance, D('0.00'))
        self.assertEqual(bal(WHT_OUT), D('0.00'))

        expense = PrintTransaction(treasury=self.treasury, transaction_type='out', amount=D('50'),
                                   category='rent', withholding_tax=D('1'))
        with self.assertRaises(ValidationError):
            expense.full_clean()

    # ── 3. الشيكات ────────────────────────────────────────────────────────
    def test_incoming_cheque_pays_order_then_clears_or_bounces(self):
        order = self._delivered('2000')
        cheque = PrintCheque.objects.create(direction='in', number='100200', bank_name='CIB', amount=D('2000'),
                                            due_date=self.today + timedelta(days=30), order=order)
        order.refresh_from_db()
        self.assertEqual(order.paid_amount, D('2000.00'))
        self.assertEqual(bal(CHEQ_IN), D('2000.00'))
        self.assertEqual(bal(AR), D('0.00'))
        with self.assertRaises(ValidationError):
            cheque.txn.delete()
        cheque.clear(self.bank, user=self.user)
        self.bank.refresh_from_db()
        self.assertEqual((cheque.status, self.bank.balance), ('cleared', D('12000.00')))
        self.assertEqual(bal(CHEQ_IN), D('0.00'))
        with self.assertRaises(ValidationError):
            cheque.delete()

        order2 = self._delivered('700')
        bounced = PrintCheque.objects.create(direction='in', number='555', amount=D('700'),
                                             due_date=self.today, order=order2)
        bounced.bounce(reason='رصيد غير كافي')
        order2.refresh_from_db()
        self.assertEqual((bounced.status, order2.paid_amount), ('bounced', D('0.00')))
        self.assertEqual(bal(AR), D('700.00'))
        self.assertEqual(bal(CHEQ_IN), D('0.00'))

        mistake = PrintCheque.objects.create(direction='in', number='999', amount=D('50'),
                                             due_date=self.today, customer=self.customer)
        mistake.delete()
        self.assertEqual(PrintTreasury.objects.get(kind='cheques_in').balance, D('0.00'))

    def test_outgoing_cheque_pays_supplier_until_cleared(self):
        p = self._purchase('30', '100')
        cheque = PrintCheque.objects.create(direction='out', number='7001', amount=D('3000'),
                                            due_date=self.today + timedelta(days=15), purchase=p)
        p.refresh_from_db()
        self.assertEqual(p.remaining, D('0.00'))
        self.assertEqual(bal(CHEQ_OUT), D('-3000.00'))
        self.assertEqual(PrintTreasury.objects.get(kind='cheques_out').balance, D('-3000.00'))
        cheque.clear(self.bank)
        self.bank.refresh_from_db()
        self.assertEqual(self.bank.balance, D('7000.00'))
        self.assertEqual(bal(CHEQ_OUT), D('0.00'))

        p2 = self._purchase('5', '100')
        bad = PrintCheque.objects.create(direction='out', number='7002', amount=D('500'), due_date=self.today,
                                         purchase=p2)
        bad.bounce()
        p2.refresh_from_db()
        self.assertEqual(p2.remaining, D('500.00'))
        self.assertEqual(self.supplier.account_summary()['balance'], D('500.00'))

    def test_cheque_admin_settle_page(self):
        order = self._delivered('400')
        cheque = PrintCheque.objects.create(direction='in', number='42', amount=D('400'), due_date=self.today,
                                            order=order)
        url = f'/secure-portal/printing/printcheque/{cheque.pk}/settle/'
        self.assertEqual(self.get(url).status_code, 200)
        r = self.post(url, {'action': 'clear', 'treasury': self.bank.pk, 'date': self.today.isoformat()})
        self.assertEqual(r.status_code, 302)
        cheque.refresh_from_db()
        self.assertEqual(cheque.status, 'cleared')

    # ── 4. الإعدام والخصم والتسديد من رصيد المورد ───────────────────────
    def test_discount_and_bad_debt_are_expenses_not_returns(self):
        order = self._delivered('1000')
        PrintTransaction.objects.create(treasury=self.treasury, transaction_type='in', amount=D('995'), order=order)
        r = self.post('/secure-portal/printing/printorder/', {'action': 'write_off_remaining',
                                                              '_selected_action': [order.pk]})
        self.assertEqual(r.status_code, 302)
        order.refresh_from_db()
        self.assertEqual(order.remaining, D('0.00'))
        self.assertEqual(bal(DISCOUNT), D('5.00'))
        self.assertEqual(bal('4090'), D('0'))                       # مش مرتجع مبيعات

        order2 = self._delivered('300')
        PrintCreditNote.objects.create(order=order2, kind='bad_debt', amount=D('300'), reason='مش هيتحصّل')
        self.assertEqual(bal(BAD_DEBTS), D('300.00'))
        for bad in (dict(order=order2, kind='discount', amount=D('1'), tax_amount=D('1')),
                    dict(customer=self.customer, kind='return', amount=D('1')),
                    dict(order=order, kind='bad_debt', amount=D('1'), refund_treasury=self.treasury)):
            with self.assertRaises(ValidationError):
                PrintCreditNote.objects.create(reason='x', **bad)

    def test_supplier_credit_settles_open_invoices(self):
        PrintTransaction.objects.create(treasury=self.treasury, transaction_type='out', amount=D('1000'),
                                        supplier=self.supplier)
        p = self._purchase('6', '100')
        bill = PrintExpenseBill.objects.create(supplier=self.supplier, category='maintenance',
                                               description='صيانة', amount=D('300'))
        self.assertEqual(p.remaining, D('600.00'))
        r = self.post('/secure-portal/printing/printsupplier/', {'action': 'settle_docs_from_credit',
                                                                 '_selected_action': [self.supplier.pk]})
        self.assertEqual(r.status_code, 302)
        p.refresh_from_db()
        bill.refresh_from_db()
        self.assertEqual((p.remaining, bill.remaining), (D('0.00'), D('0.00')))
        self.assertEqual(self.supplier.available_credit(), D('100.00'))
        self.assertEqual(self.supplier.account_summary()['balance'], D('-100.00'))
        self.assertEqual(bal(SUP_ADV), D('100.00'))
        with self.assertRaises(ValidationError):
            PrintSupplierAllocation.objects.create(purchase=p, amount=D('1'))
        self.assertEqual(payables_aging(self.today)['total'], D('0'))

    # ── 5. الفروع ──────────────────────────────────────────────────────────
    def test_income_by_branch_and_branch_filter(self):
        north = PrintBranch.objects.create(name='فرع المهندسين')
        south = PrintBranch.objects.create(name='فرع المعادي')
        south_drawer = PrintTreasury.objects.create(name='خزنة المعادي', branch=south, balance=D('1000'))
        self._delivered('1000', branch=north)
        PrintTransaction.objects.create(treasury=south_drawer, transaction_type='out', amount=D('300'),
                                        category='rent', description='إيجار المعادي')
        PrintExpenseBill.objects.create(supplier=self.supplier, branch=north, category='utilities',
                                        description='كهرباء', amount=D('100'))
        rows = {(r['branch'].name if r['branch'] else None): r
                for r in ledger.income_by_branch(self.today.replace(day=1), self.today)}
        self.assertEqual((rows['فرع المهندسين']['revenue'], rows['فرع المهندسين']['net']),
                         (D('1000.00'), D('900.00')))
        self.assertEqual(rows['فرع المعادي']['net'], D('-300.00'))
        r = self.get(f'/printing/reports/profit-loss/?branch={north.pk}')
        self.assertEqual((r.context['total_income'], r.context['net_profit']), (D('1000.00'), D('900.00')))
        r = self.get('/printing/accounting/?tab=branches')
        self.assertContains(r, 'فرع المعادي')
        self.assertEqual(self.get('/printing/reports/profit-loss/?branch=none').status_code, 200)

    def test_old_ledger_gets_branch_tags_on_upgrade(self):
        branch = PrintBranch.objects.create(name='فرع الدقي')
        order = self._delivered('500', branch=branch)
        PrintJournalBranch.objects.all().delete()
        PrintLedgerState.objects.update_or_create(pk=1, defaults={'bootstrapped_at': timezone.now(), 'version': 2})
        ledger.ensure_bootstrapped()
        self.assertEqual(PrintLedgerState.get().version, ledger.LEDGER_VERSION)
        je = JournalEntry.objects.get(reference=ledger.ref_order(order.pk))
        self.assertEqual(je.print_branch.branch, branch)

    def test_depreciation_is_split_by_branch(self):
        branch = PrintBranch.objects.create(name='فرع مدينة نصر')
        # 🐛 [FIX]: الاختبار كان بيقع آخر يوم في أي شهر — النهارده بيبقى «شهر خلص» فبيتقيّد شهرين
        #    (300 بدل 150). نبدأ من آخر شهر خلص فعلاً فيتقيّد قسط شهر واحد أياً كان اليوم.
        start = ledger.last_completed_month_end(self.today).replace(day=1)
        PrintFixedAsset.objects.create(name='ماكينة', cost=D('1200'), useful_life_months=12, branch=branch,
                                       purchase_date=start, funding='credit', supplier=self.supplier)
        PrintFixedAsset.objects.create(name='كمبيوتر', cost=D('600'), useful_life_months=12,
                                       purchase_date=start, funding='credit', supplier=self.supplier)
        ref = ledger.ref_depreciation(start.year, start.month)
        self.assertTrue(JournalEntry.objects.filter(reference=f'{ref}-B{branch.pk}').exists())
        self.assertTrue(JournalEntry.objects.filter(reference=ref).exists())
        self.assertEqual(bal('5600'), D('150.00'))

    # ── 6. كارت الصنف + Excel ──────────────────────────────────────────────
    def test_stock_card_runs_to_current_quantity(self):
        p = self._purchase('20', '110')
        order = PrintOrder.objects.create(customer=self.customer, status='confirmed')
        job = PrintJob.objects.create(order=order, description='كتب', quantity=1, unit_price=D('5000'),
                                      proof_waived=True)
        PrintJobMaterial.objects.create(job=job, material=self.paper, quantity=D('5'))
        PrintStockAdjustment.objects.create(material=self.paper, quantity_change=D('-1'), reason='damage')
        ret = MaterialPurchaseReturn.objects.create(purchase=p)
        MaterialPurchaseReturnLine.objects.create(ret=ret, purchase_line=p.lines.get(), quantity=D('4'))
        ret.post()
        self.paper.refresh_from_db()
        opening, moves = material_movements(self.paper)
        self.assertEqual(opening, D('10'))
        self.assertEqual(opening + sum(m['qty'] for m in moves), self.paper.quantity)
        self.assertEqual(self.paper.quantity, D('20'))
        r = self.get(f'/printing/material/{self.paper.pk}/card/')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.context['rows']), 4)
        r = self.get(f'/printing/material/{self.paper.pk}/card/?export=xlsx')
        self.assertEqual(r['Content-Type'], XLSX)

    def test_excel_exports(self):
        self._delivered('800')
        urls = ['/printing/accounting/?tab=trial&export=xlsx', '/printing/accounting/?tab=aging&export=xlsx',
                '/printing/accounting/?tab=branches&export=xlsx',
                f'/printing/customer/{self.customer.pk}/statement/?export=xlsx',
                f'/printing/supplier/{self.supplier.pk}/statement/?export=xlsx']
        for url in urls:
            r = self.get(url)
            self.assertEqual((r.status_code, r['Content-Type']), (200, XLSX), url)
            self.assertTrue(r.content.startswith(b'PK'), url)

    # ── 7. الشاشات ────────────────────────────────────────────────────────
    def test_admin_pages_and_virtual_wallets_hidden_from_payment_pickers(self):
        from printing.admin import PrintTransactionForm
        order = self._delivered('600')
        cheque = PrintCheque.objects.create(direction='in', number='1', amount=D('100'), due_date=self.today,
                                            order=order)
        base = '/secure-portal/printing'
        for url in (f'{base}/printcheque/', f'{base}/printcheque/add/', f'{base}/printcheque/{cheque.pk}/change/',
                    f'{base}/printsupplierallocation/', f'{base}/printcreditnote/add/',
                    f'{base}/printcustomer/{self.customer.pk}/change/', f'{base}/printsupplier/{self.supplier.pk}/change/',
                    f'{base}/printorder/{order.pk}/change/', f'{base}/printtreasury/',
                    f'{base}/printtreasury/{cheque.txn.treasury_id}/change/', f'{base}/printmaterial/'):
            self.assertEqual(self.get(url).status_code, 200, url)
        kinds = set(PrintTransactionForm().fields['treasury'].queryset.values_list('kind', flat=True))
        self.assertTrue(kinds <= set(PrintTreasury.MONEY_KINDS))
        r = self.post(f'{base}/printtreasurytransfer/add/', {
            'from_treasury': cheque.txn.treasury_id, 'to_treasury': self.bank.pk, 'amount': '100',
            'date_0': self.today.isoformat(), 'date_1': '10:00:00'})
        self.assertEqual(r.status_code, 200)                      # الشيك بيتصرف من شاشته بس
        self.assertFalse(PrintTreasuryTransfer.objects.exists())

        r = self.post(f'{base}/printcheque/add/', {
            'direction': 'in', 'number': '3030', 'bank_name': 'QNB', 'issue_date': self.today.isoformat(),
            'due_date': (self.today + timedelta(days=10)).isoformat(), 'amount': '500', 'order': order.pk})
        self.assertEqual(r.status_code, 302)
        order.refresh_from_db()
        self.assertEqual(order.paid_amount, D('600.00'))
