"""
⚡ شاشات الإدخال السريع: تحصيل من عميل، فاتورة شراء خامات، عميل جديد (printing/quick_entry.py).

كل واحدة لازم تقيّد بالظبط زي شاشتها المفصّلة: التحصيل يقلّل الباقي على الفاتورة ويدخل الخزنة،
الشراء يزوّد المخزن بمتوسط التكلفة ويخصم الكاش أو يفضل على المورد، والرصيد القديم للعميل
رصيد افتتاحي — والدفاتر متزنة وكل خزنة = دفترها بعد كل اختبار (tearDown).
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import connection
from django.db.models import Q, Sum
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from inventory.models import AccountingEntry
from inventory.services.accounting_reports import AccountingReportService
from inventory.tests.base import ERPTenantTestCase
from printing import ledger
from printing.models import (
    MaterialPurchase, PrintCustomer, PrintJob, PrintMaterial, PrintOrder, PrintSupplier, PrintTransaction,
    PrintTreasury,
)

User = get_user_model()
D = Decimal


def bal(code):
    agg = (AccountingEntry.objects.filter(Q(account__code=code) | Q(account__parent__code=code))
           .aggregate(d=Sum('debit'), c=Sum('credit')))
    return (agg['d'] or D('0')) - (agg['c'] or D('0'))


@override_settings(ALLOWED_HOSTS=['*'])
class QuickEntryBase(ERPTenantTestCase):

    def setUp(self):
        super().setUp()
        connection.set_tenant(self.tenant)
        type(self.tenant).objects.filter(pk=self.tenant.pk).update(industry='printing')
        self.host = self.domain.domain
        self.treasury = PrintTreasury.objects.create(name='الخزنة')
        self.user = User.objects.create_superuser('entry_boss', 'e@x.com', 'x')
        self.c = Client()
        self.c.force_login(self.user)
        self.today = timezone.localdate()

    def tearDown(self):
        connection.set_tenant(self.tenant)
        tb = AccountingReportService.trial_balance()
        self.assertTrue(tb['is_balanced'], tb)
        for t in PrintTreasury.objects.all():
            agg = ledger.treasury_account(t).entries.aggregate(d=Sum('debit'), c=Sum('credit'))
            self.assertEqual((agg['d'] or D('0')) - (agg['c'] or D('0')), t.balance, t.name)
        super().tearDown()

    def capital(self, amount):
        PrintTransaction.objects.create(treasury=self.treasury, transaction_type='in', category='capital',
                                        amount=D(amount), description='رأس مال')
        self.treasury.refresh_from_db()

    def delivered(self, customer, price):
        order = PrintOrder.objects.create(customer=customer, status='confirmed')
        PrintJob.objects.create(order=order, description='كروت', quantity=1, unit_price=D(price), proof_waived=True)
        order.refresh_from_db()
        order.status = 'delivered'
        order.save()
        order.refresh_from_db()
        return order


class QuickCollectTests(QuickEntryBase):

    def setUp(self):
        super().setUp()
        self.cust = PrintCustomer.objects.create(name='شركة الندى', phone='01011112222')
        self.url = reverse('printing:quick_collect')

    def post(self, **data):
        payload = {'customer': str(self.cust.pk), 'treasury': str(self.treasury.pk), 'amount': '',
                   'withholding_tax': '', 'description': ''}
        payload.update(data)
        return self.c.post(self.url, payload, HTTP_HOST=self.host)

    def test_customer_list_puts_debtors_first(self):
        PrintCustomer.objects.create(name='أ — مفيش عليه', phone='01000000001')
        self.delivered(self.cust, '700')
        r = self.c.get(self.url, HTTP_HOST=self.host)
        html = r.content.decode()
        self.assertLess(html.index('شركة الندى'), html.index('أ — مفيش عليه'))
        self.assertIn('700.00', html)

    def test_partial_then_full_collection_on_an_invoice(self):
        order = self.delivered(self.cust, '1000')
        r = self.c.get(f'{self.url}?customer={self.cust.pk}', HTTP_HOST=self.host)
        self.assertContains(r, f'value="{order.pk}" data-remaining="1000.00" checked')   # أقدم فاتورة مختارة
        r = self.post(order=str(order.pk), amount='400')
        self.assertContains(r, 'اتسجّل التحصيل')
        order.refresh_from_db()
        self.assertEqual(order.remaining, D('600.00'))
        self.assertEqual(bal('1100'), D('600.00'))
        self.post(order=str(order.pk), amount='600')
        order.refresh_from_db()
        self.treasury.refresh_from_db()
        self.assertEqual((order.remaining, self.treasury.balance), (D('0.00'), D('1000.00')))
        self.assertEqual(bal('1100'), D('0.00'))
        txn = PrintTransaction.objects.filter(order=order).first()
        self.assertEqual((txn.category, txn.customer_id, txn.transaction_type), ('customer', self.cust.pk, 'in'))

    def test_more_than_the_invoice_remaining_is_refused(self):
        order = self.delivered(self.cust, '300')
        r = self.post(order=str(order.pk), amount='301')
        self.assertContains(r, 'المبلغ أكبر من الباقي على الفاتورة')
        self.assertContains(r, 'value="301"')                                  # المبلغ مااتمسحش
        self.assertFalse(PrintTransaction.objects.exists())

    def test_on_account_payment_becomes_customer_credit(self):
        r = self.post(order='account', amount='250')
        self.assertContains(r, 'اتسجّل التحصيل')
        self.assertEqual(self.cust.account_summary()['balance'], D('-250.00'))  # ليه رصيد
        # الدفتر: الرصيد المقدّم «دفعات مقدمة من العملاء» (التزام 2400)، مش ذمم سالبة
        self.assertEqual((bal('1100'), bal('2400')), (D('0.00'), D('-250.00')))

    def test_withholding_counts_toward_the_invoice(self):
        order = self.delivered(self.cust, '1000')
        self.post(order=str(order.pk), amount='990', withholding_tax='10')
        order.refresh_from_db()
        self.treasury.refresh_from_db()
        self.assertEqual((order.remaining, self.treasury.balance), (D('0.00'), D('990.00')))

    def test_someone_elses_invoice_is_refused(self):
        other = PrintCustomer.objects.create(name='تاني', phone='01099990000')
        order = self.delivered(other, '500')
        r = self.post(order=str(order.pk), amount='100')
        self.assertContains(r, 'مش بتاعة العميل ده')
        self.assertFalse(PrintTransaction.objects.exists())

    def test_zero_or_missing_amount_is_refused(self):
        for amount in ('', '0', '-5', 'abc'):
            r = self.post(order='account', amount=amount)
            self.assertEqual(r.status_code, 200)
            self.assertContains(r, 'alert-danger')
        self.assertFalse(PrintTransaction.objects.exists())

    def test_invoice_page_links_to_collect_the_rest(self):
        order = self.delivered(self.cust, '500')
        r = self.c.get(reverse('printing:order_invoice', args=[order.pk]), HTTP_HOST=self.host)
        self.assertContains(r, f'{self.url}?customer={self.cust.pk}&amp;order={order.pk}')

    def test_needs_permission(self):
        clerk = User.objects.create_user('clerk', 'c@x.com', 'x', is_staff=True)
        c = Client()
        c.force_login(clerk)
        self.assertEqual(c.get(self.url, HTTP_HOST=self.host).status_code, 403)


class QuickPurchaseTests(QuickEntryBase):

    def setUp(self):
        super().setUp()
        self.paper = PrintMaterial.objects.create(name='ورق A4', unit='رزمة', quantity=D('10'), cost_per_unit=D('100'))
        self.url = reverse('printing:quick_purchase')

    def post(self, **data):
        payload = {'supplier_name': 'مورد الورق', 'supplier_phone': '01077778888', 'supplier_id': '',
                   'line_material': [str(self.paper.pk)], 'line_name': ['ورق A4'], 'line_unit': [''],
                   'line_qty': ['10'], 'line_cost': ['120'], 'payment_terms': 'cash',
                   'treasury': str(self.treasury.pk), 'receive': '1', 'discount': '', 'tax': '',
                   'supplier_invoice_no': '', 'notes': ''}
        payload.update(data)
        return self.c.post(self.url, payload, HTTP_HOST=self.host)

    def test_cash_purchase_received_moves_stock_cost_and_cash(self):
        self.capital('5000')
        r = self.post()
        self.assertContains(r, 'الخامات دخلت المخزن')
        p = MaterialPurchase.objects.get()
        self.assertEqual((p.status, p.payment_terms, p.paid_amount), ('received', 'cash', D('1200.00')))
        self.paper.refresh_from_db()
        self.assertEqual(self.paper.quantity, D('20.00'))
        self.assertEqual(self.paper.cost_per_unit, D('110.0000'))              # (10×100 + 10×120) / 20
        self.treasury.refresh_from_db()
        self.assertEqual(self.treasury.balance, D('3800.00'))
        self.assertEqual(PrintSupplier.objects.get().phone, '01077778888')

    def test_credit_purchase_stays_on_the_supplier(self):
        self.post(payment_terms='credit', treasury='')
        p = MaterialPurchase.objects.get()
        self.assertEqual((p.status, p.paid_amount, p.remaining), ('received', D('0.00'), D('1200.00')))
        self.assertEqual(bal('2100'), D('-1200.00'))                           # الموردين لهم 1200
        self.assertFalse(PrintTransaction.objects.exists())

    def test_not_enough_cash_saves_nothing(self):
        self.capital('100')
        r = self.post()
        self.assertContains(r, 'مش كفاية')
        self.assertFalse(MaterialPurchase.objects.exists())
        self.assertFalse(PrintSupplier.objects.exists())                       # ولا مورد نص-متسجّل
        self.paper.refresh_from_db()
        self.assertEqual(self.paper.quantity, D('10.00'))
        self.treasury.refresh_from_db()
        self.assertEqual(self.treasury.balance, D('100.00'))

    def test_new_material_by_name_with_fractional_quantity(self):
        self.capital('1000')
        r = self.post(line_material=[''], line_name=['فينيل لامع'], line_unit=['متر'], line_qty=['2.5'], line_cost=['40'])
        self.assertContains(r, 'اتضافت خامة جديدة')
        vinyl = PrintMaterial.objects.get(name='فينيل لامع')
        self.assertEqual((vinyl.unit, vinyl.quantity, vinyl.cost_per_unit), ('متر', D('2.50'), D('40.0000')))
        self.treasury.refresh_from_db()
        self.assertEqual(self.treasury.balance, D('900.00'))

    def test_draft_does_not_touch_stock_or_cash(self):
        self.capital('5000')
        self.post(receive='0')
        p = MaterialPurchase.objects.get()
        self.assertEqual(p.status, 'draft')
        self.paper.refresh_from_db()
        self.treasury.refresh_from_db()
        self.assertEqual((self.paper.quantity, self.treasury.balance), (D('10.00'), D('5000.00')))

    def test_discount_and_tax(self):
        self.capital('5000')
        self.post(discount='200', tax='140')
        p = MaterialPurchase.objects.get()
        self.assertEqual((p.inventory_value, p.net_total, p.paid_amount), (D('1000.00'), D('1140.00'), D('1140.00')))
        self.paper.refresh_from_db()
        self.assertEqual(self.paper.cost_per_unit, D('100.0000'))              # الخصم بيقلّل التكلفة، الضريبة لأ

    def test_existing_supplier_is_reused(self):
        sup = PrintSupplier.objects.create(name='مورد الورق', phone='01077778888')
        self.post(payment_terms='credit', treasury='', supplier_phone='')
        self.post(payment_terms='credit', treasury='', supplier_id=str(sup.pk))
        self.assertEqual(PrintSupplier.objects.count(), 1)
        self.assertEqual(MaterialPurchase.objects.filter(supplier=sup).count(), 2)

    def test_bad_lines_are_refused(self):
        for data, msg in (
            ({'line_qty': ['0']}, 'لازم تكون أكبر من صفر'),
            ({'line_material': [''], 'line_name': ['']}, 'اختار الخامة أو اكتب اسمها'),
            ({'line_material': [''], 'line_name': [''], 'line_qty': [''], 'line_cost': ['']}, 'ضيف خامة واحدة على الأقل'),
            ({'line_cost': ['-1']}, 'لازم يكون صفر أو أكتر'),
            ({'line_cost': ['0']}, 'إجمالي الفاتورة صفر'),
            ({'discount': '99999'}, 'الخصم أكبر من إجمالي البنود'),
            ({'treasury': ''}, 'اختار الخزنة'),
        ):
            r = self.post(**data)
            self.assertContains(r, msg, msg_prefix=str(data))
        self.assertFalse(MaterialPurchase.objects.exists())


class QuickCustomerTests(QuickEntryBase):

    def setUp(self):
        super().setUp()
        self.url = reverse('printing:quick_customer')

    def post(self, **data):
        payload = {'name': 'مكتبة النور', 'phone': '01033334444', 'company': '', 'whatsapp': '',
                   'opening': 'none', 'opening_amount': '', 'opening_date': '', 'credit_limit': '', 'notes': ''}
        payload.update(data)
        return self.c.post(self.url, payload, HTTP_HOST=self.host)

    def test_name_and_phone_is_enough(self):
        r = self.post()
        cust = PrintCustomer.objects.get()
        self.assertContains(r, 'اتضاف العميل')
        self.assertContains(r, f"{reverse('printing:quick_invoice')}?customer={cust.pk}")
        self.assertEqual((cust.phone, cust.whatsapp, cust.opening_balance), ('01033334444', '01033334444', D('0')))

    def test_old_debt_is_an_opening_balance_not_revenue(self):
        self.post(opening='owes', opening_amount='750', opening_date='2026-01-01')
        cust = PrintCustomer.objects.get()
        self.assertEqual((cust.opening_balance, str(cust.opening_date)), (D('750.00'), '2026-01-01'))
        self.assertEqual(cust.account_summary()['balance'], D('750.00'))
        self.assertEqual(bal('1100'), D('750.00'))
        pnl = AccountingReportService.income_statement(self.today.replace(day=1), self.today)
        self.assertEqual(pnl['revenue']['total'], D('0'))

    def test_old_credit_is_negative(self):
        self.post(opening='credit', opening_amount='200')
        self.assertEqual(PrintCustomer.objects.get().opening_balance, D('-200.00'))

    def test_opening_without_amount_is_refused(self):
        r = self.post(opening='owes', opening_amount='')
        self.assertContains(r, 'اكتب المبلغ')
        self.assertFalse(PrintCustomer.objects.exists())

    def test_duplicate_phone_and_missing_name_are_refused(self):
        PrintCustomer.objects.create(name='قديم', phone='01033334444')
        self.assertContains(self.post(), 'فيه عميل متسجّل بنفس الرقم')
        self.assertContains(self.post(name='', phone='01055550000'), 'اكتب اسم العميل')
        self.assertEqual(PrintCustomer.objects.count(), 1)

    def test_quick_invoice_opens_with_the_customer(self):
        self.post()
        cust = PrintCustomer.objects.get()
        r = self.c.get(f"{reverse('printing:quick_invoice')}?customer={cust.pk}", HTTP_HOST=self.host)
        self.assertContains(r, f'name="customer_id" id="qiCustomerId" value="{cust.pk}"')
        self.assertContains(r, 'value="مكتبة النور"')
        self.assertContains(r, 'name="delivered" value="1" checked')

    def test_dashboard_points_to_the_quick_screens(self):
        dash = self.c.get(reverse('admin:index'), HTTP_HOST=self.host).content.decode()
        for name in ('quick_collect', 'quick_purchase', 'quick_customer'):
            self.assertIn(reverse(f'printing:{name}'), dash, name)


class QuickExpenseTests(QuickEntryBase):

    def setUp(self):
        super().setUp()
        self.url = reverse('printing:quick_expense')

    def post(self, **data):
        payload = {'amount': '800', 'category': 'rent', 'treasury': str(self.treasury.pk), 'description': 'إيجار نوفمبر'}
        payload.update(data)
        return self.c.post(self.url, payload, HTTP_HOST=self.host)

    def test_expense_leaves_the_treasury_and_hits_profit(self):
        self.capital('1000')
        r = self.post()
        self.assertContains(r, 'اتسجّل المصروف')
        txn = PrintTransaction.objects.get(category='rent')
        self.assertEqual((txn.transaction_type, txn.amount), ('out', D('800.00')))
        self.treasury.refresh_from_db()
        self.assertEqual(self.treasury.balance, D('200.00'))
        pnl = AccountingReportService.income_statement(self.today.replace(day=1), self.today)
        self.assertEqual(pnl['expenses']['total'], D('800.00'))

    def test_not_enough_cash_or_missing_fields_save_nothing(self):
        self.capital('100')
        for data, msg in (({}, 'لا يكفي'), ({'category': ''}, 'اختار بند المصروف'),
                          ({'category': 'capital'}, 'اختار بند المصروف'), ({'amount': ''}, 'اكتب مبلغ المصروف'),
                          ({'treasury': ''}, 'اختار الخزنة')):
            r = self.post(**data)
            self.assertContains(r, msg, msg_prefix=str(data))
        self.assertEqual(PrintTransaction.objects.count(), 1)                   # رأس المال بس
        self.treasury.refresh_from_db()
        self.assertEqual(self.treasury.balance, D('100.00'))


class QuickPaySupplierTests(QuickEntryBase):

    def setUp(self):
        super().setUp()
        self.sup = PrintSupplier.objects.create(name='مورد الورق', phone='01077778888')
        self.paper = PrintMaterial.objects.create(name='ورق A4', unit='رزمة')
        self.url = reverse('printing:quick_pay_supplier')

    def credit_purchase(self, cost):
        from printing.models import MaterialPurchaseLine
        p = MaterialPurchase.objects.create(supplier=self.sup, payment_terms='credit')
        MaterialPurchaseLine.objects.create(purchase=p, material=self.paper, quantity=D('1'), unit_cost=D(cost))
        p.refresh_from_db()
        p.receive(user=self.user)
        p.refresh_from_db()
        return p

    def post(self, **data):
        payload = {'supplier': str(self.sup.pk), 'treasury': str(self.treasury.pk), 'amount': '', 'doc': '',
                   'withholding_tax': '', 'description': ''}
        payload.update(data)
        return self.c.post(self.url, payload, HTTP_HOST=self.host)

    def test_pay_part_then_rest_of_a_purchase(self):
        self.capital('5000')
        p = self.credit_purchase('1000')
        r = self.c.get(f'{self.url}?supplier={self.sup.pk}', HTTP_HOST=self.host)
        self.assertContains(r, f'value="purchase:{p.pk}" data-remaining="1000.00" checked')
        self.assertContains(self.post(doc=f'purchase:{p.pk}', amount='400'), 'اتسجّل السداد')
        p.refresh_from_db()
        self.assertEqual(p.remaining, D('600.00'))
        self.assertEqual(bal('2100'), D('-600.00'))
        self.post(doc=f'purchase:{p.pk}', amount='600')
        p.refresh_from_db()
        self.treasury.refresh_from_db()
        self.assertEqual((p.remaining, self.treasury.balance), (D('0.00'), D('4000.00')))
        self.assertEqual(bal('2100'), D('0.00'))

    def test_supplier_list_puts_who_we_owe_first(self):
        PrintSupplier.objects.create(name='أ — خالص', phone='01000000002')
        self.credit_purchase('700')
        html = self.c.get(self.url, HTTP_HOST=self.host).content.decode()
        self.assertLess(html.index('مورد الورق'), html.index('أ — خالص'))

    def test_more_than_remaining_or_cash_is_refused(self):
        self.capital('300')
        p = self.credit_purchase('1000')
        self.assertContains(self.post(doc=f'purchase:{p.pk}', amount='1001'), 'المبلغ أكبر من الباقي')
        self.assertContains(self.post(doc=f'purchase:{p.pk}', amount='500'), 'لا يكفي')
        other = PrintSupplier.objects.create(name='تاني', phone='01099990001')
        self.assertContains(self.c.post(self.url, {'supplier': str(other.pk), 'treasury': str(self.treasury.pk),
                                                   'amount': '100', 'doc': f'purchase:{p.pk}'}, HTTP_HOST=self.host),
                            'مش بتاعة المورد ده')
        self.assertFalse(PrintTransaction.objects.filter(category='supplier').exists())
        self.treasury.refresh_from_db()
        self.assertEqual(self.treasury.balance, D('300.00'))

    def test_on_account_advance(self):
        self.capital('1000')
        self.post(doc='account', amount='250')
        self.assertEqual(self.sup.account_summary()['balance'], D('-250.00'))  # لينا رصيد عنده


class QuickScreensMoneyVisibilityTests(QuickEntryBase):
    """🔐 موظف بيسجّل مصروف/شراء/سداد بس صلاحياته مقفول عليها الخزنة — مايشوفش أرصدة الخزن."""

    def test_treasury_balance_hidden_without_can_view_treasury(self):
        from django.contrib.auth.models import Permission
        from printing.models import StaffPermission
        self.capital('12345')
        clerk = User.objects.create_user('clerk2', 'c2@x.com', 'x', is_staff=True)
        clerk.user_permissions.add(*Permission.objects.filter(
            content_type__app_label='printing', codename__in=['add_printtransaction', 'add_materialpurchase']))
        StaffPermission.objects.create(user=clerk, can_view_treasury=False)
        c = Client()
        c.force_login(clerk)
        for name in ('quick_expense', 'quick_purchase', 'quick_pay_supplier'):
            html = c.get(reverse(f'printing:{name}') + ('?supplier=' + str(PrintSupplier.objects.create(
                name=f's-{name}').pk) if name == 'quick_pay_supplier' else ''), HTTP_HOST=self.host).content.decode()
            self.assertIn(self.treasury.name, html, name)
            self.assertNotIn('12,345', html, name)
            self.assertNotIn('data-balance=', html, name)
        # والمالك بيشوفه
        html = self.c.get(reverse('printing:quick_expense'), HTTP_HOST=self.host).content.decode()
        self.assertIn('12,345', html)


class PurchasePayRestLinkTests(QuickEntryBase):

    def test_credit_purchase_page_offers_pay_the_rest(self):
        self.c.post(reverse('printing:quick_purchase'), {
            'supplier_name': 'مورد', 'supplier_phone': '', 'supplier_id': '', 'line_material': [''],
            'line_name': ['ورق'], 'line_unit': ['رزمة'], 'line_qty': ['2'], 'line_cost': ['50'],
            'payment_terms': 'credit', 'treasury': '', 'receive': '1'}, HTTP_HOST=self.host)
        p = MaterialPurchase.objects.get()
        link = f"{reverse('printing:quick_pay_supplier')}?supplier={p.supplier_id}&amp;doc=purchase:{p.pk}"
        page = self.c.get(reverse('admin:printing_materialpurchase_change', args=[p.pk]), HTTP_HOST=self.host)
        self.assertContains(page, link)
        self.assertNotContains(page, '&lt;a href')                            # اللينك مش متعمله escape
        r = self.c.get(f"{reverse('printing:quick_pay_supplier')}?supplier={p.supplier_id}&doc=purchase:{p.pk}",
                       HTTP_HOST=self.host)
        self.assertContains(r, f'value="purchase:{p.pk}" data-remaining="100.00" checked')


class PartyImportTests(QuickEntryBase):
    """👥 استيراد عملاء/موردين المطبعة بأرصدتهم (printing/party_import.py) — الرصيد افتتاحي مش إيراد."""

    def _ds(self, headers, rows):
        import tablib
        d = tablib.Dataset(headers=headers)
        for r in rows:
            d.append(r)
        return d

    def test_customers_opening_balances_go_to_receivables_once(self):
        from printing.party_import import import_print_parties
        f = self._ds(['اسم العميل', 'الموبايل', 'المديونية', 'الشركة'],
                     [['شركة النور', '1012345678.0', '3500', 'النور'], ['أحمد', '01198765432', '-200', '']])
        r = import_print_parties('customer', f)
        self.assertEqual((r['created'], r['balances'], r['error_count']), (2, 2, 0))
        nour = PrintCustomer.objects.get(phone='01012345678')
        self.assertEqual((nour.opening_balance, nour.company, nour.balance), (D('3500.00'), 'النور', D('3500.00')))
        # ٣٥٠٠ عليه ⇒ العملاء (١١٠٠)، و٢٠٠ ليه ⇒ دفعات العملاء المقدّمة (٢٤٠٠) مش بالسالب في ١١٠٠
        self.assertEqual((bal('1100'), bal('2400')), (D('3500.00'), D('-200.00')))
        # نفس الملف تاني ⇒ مفيش تكرار
        r = import_print_parties('customer', f)
        self.assertEqual((r['created'], r['updated'], r['balances']), (0, 2, 0))
        self.assertEqual((bal('1100'), bal('2400')), (D('3500.00'), D('-200.00')))
        # تعديل الرصيد ⇒ القيد بيتزامن على الرقم الجديد
        import_print_parties('customer', self._ds(['name', 'phone', 'balance'], [['شركة النور', '01012345678', '1000']]))
        self.assertEqual(bal('1100'), D('1000.00'))

    def test_suppliers_by_name_and_bad_rows(self):
        from printing.party_import import import_print_parties
        r = import_print_parties('supplier', self._ds(['المورد', 'رصيد'],
                                                       [['مخازن الورق', '12000'], ['أحبار', 'كتير'], ['', '']]))
        self.assertEqual((r['created'], r['error_count']), (1, 1))
        self.assertEqual(bal('2100'), D('-12000.00'))
        sup = PrintSupplier.objects.get(name='مخازن الورق')
        self.assertEqual(sup.balance, D('12000.00'))
        # خانة الرصيد الفاضية ماتلمسش الرصيد
        import_print_parties('supplier', self._ds(['name', 'phone', 'balance'], [['مخازن الورق', '01022223333', '']]))
        sup.refresh_from_db()
        self.assertEqual((sup.phone, sup.opening_balance), ('01022223333', D('12000.00')))

    def test_page_template_upload_and_link(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        url = reverse('printing:party_import')
        r = self.c.get(url + '?kind=supplier&template=1', HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 200)
        self.assertIn('name,phone,balance,company', r.content.decode('utf-8-sig'))
        up = SimpleUploadedFile('c.csv', 'الاسم,الموبايل,الرصيد\nعميل الملف,01055556666,900\n'.encode('utf-8'))
        r = self.c.post(url, {'kind': 'customer', 'file': up}, HTTP_HOST=self.host)
        self.assertContains(r, 'id="partyImportDone"')
        self.assertEqual(PrintCustomer.objects.get(phone='01055556666').opening_balance, D('900.00'))
        self.assertContains(self.c.get(reverse('printing:quick_customer'), HTTP_HOST=self.host), url)
