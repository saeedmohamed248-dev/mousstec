"""
🧭 شغل اليوم في المطبعة من غير لخبطة — والدورة المحاسبية كاملة وراه.

🐛 صاحب المطبعة كان بيدخل اللوحة ومايلاقيش «مصروف» ولا «تحصيل» ولا «فاتورة شراء»: كله
كان فورم واحد اسمه «حركة مالية» فيه 15 خانة (النوع والبند والعميل والمورد والطلب والفاتورة…)،
والمشتريات كانت آخر قايمة أبجدية من 30 شاشة.

* شاشات بسيطة (?mode=expense / income / collect / pay_supplier) — خاناتها بس، والنوع والبند منها.
* لوحة «شغل اليوم» بترتيب الدورة + أرقام الشهر بالاستحقاق + «ابدأ هنا» للشركة الجديدة.
* يوم كامل من الشاشات دي: رأس مال ← شراء خامات ← فاتورة لعميل وتسليمها ← تحصيل ← مصروف ←
  سداد مورد — والدفاتر متزنة وقائمة الدخل صح.
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
    MaterialPurchase, MaterialPurchaseLine, PrintCustomer, PrintJob, PrintMaterial, PrintOrder, PrintSupplier,
    PrintTransaction, PrintTreasury,
)

User = get_user_model()
D = Decimal


def bal(code):
    agg = (AccountingEntry.objects.filter(Q(account__code=code) | Q(account__parent__code=code))
           .aggregate(d=Sum('debit'), c=Sum('credit')))
    return (agg['d'] or D('0')) - (agg['c'] or D('0'))


@override_settings(ALLOWED_HOSTS=['*'])
class PrintingDailyCycleUXTests(ERPTenantTestCase):

    def setUp(self):
        super().setUp()
        connection.set_tenant(self.tenant)
        type(self.tenant).objects.filter(pk=self.tenant.pk).update(industry='printing')
        self.host = self.domain.domain
        self.treasury = PrintTreasury.objects.create(name='الخزنة')
        self.customer = PrintCustomer.objects.create(name='شركة الندى', phone='01011112222')
        self.supplier = PrintSupplier.objects.create(name='مورد الورق', phone='01099998888')
        self.paper = PrintMaterial.objects.create(name='ورق 80 جم', unit='رزمة')
        self.user = User.objects.create_superuser('cycle_boss', 'c@x.com', 'x')
        self.c = Client()
        self.c.force_login(self.user)
        self.add_url = reverse('admin:printing_printtransaction_add')
        self.today = timezone.localdate()

    def tearDown(self):
        connection.set_tenant(self.tenant)
        tb = AccountingReportService.trial_balance()
        self.assertTrue(tb['is_balanced'], tb)
        for t in PrintTreasury.objects.all():      # كل خزنة = حسابها في الدفتر
            agg = ledger.treasury_account(t).entries.aggregate(d=Sum('debit'), c=Sum('credit'))
            self.assertEqual((agg['d'] or D('0')) - (agg['c'] or D('0')), t.balance, t.name)
        super().tearDown()

    def guided(self, mode, **data):
        payload = {'date_0': self.today.isoformat(), 'date_1': '10:00:00', 'description': '',
                   'treasury': self.treasury.pk, 'withholding_tax': '0', **data}
        return self.c.post(f'{self.add_url}?mode={mode}', payload, HTTP_HOST=self.host)

    def delivered(self, price):
        order = PrintOrder.objects.create(customer=self.customer, status='confirmed')
        PrintJob.objects.create(order=order, description='كروت شخصية', quantity=1, unit_price=D(price),
                                proof_waived=True)
        order.refresh_from_db()
        order.status = 'delivered'
        order.save()
        order.refresh_from_db()
        return order

    def credit_purchase(self, qty, cost):
        p = MaterialPurchase.objects.create(supplier=self.supplier, payment_terms='credit')
        MaterialPurchaseLine.objects.create(purchase=p, material=self.paper, quantity=D(qty), unit_cost=D(cost))
        p.refresh_from_db()
        p.receive(user=self.user)
        p.refresh_from_db()
        return p

    # ── الشاشات البسيطة ─────────────────────────────────────────────────
    def test_expense_screen_shows_only_its_fields(self):
        r = self.c.get(f'{self.add_url}?mode=expense', HTTP_HOST=self.host)
        self.assertContains(r, 'مصروف جديد')
        self.assertContains(r, 'name="amount"')
        self.assertContains(r, 'value="rent"')
        for hidden in ('name="transaction_type"', 'name="customer"', 'name="supplier"', 'name="order"',
                       'value="customer"', 'value="capital"'):
            self.assertNotContains(r, hidden)
        self.assertContains(r, f'<option value="{self.treasury.pk}" selected>')   # خزنة واحدة = متختارة

        # الفورم العادي لسه كامل لمحاسب عاوز كل الخانات
        full = self.c.get(self.add_url, HTTP_HOST=self.host)
        self.assertContains(full, 'name="transaction_type"')

    def test_expense_bigger_than_the_treasury_is_a_form_error(self):
        r = self.guided('expense', amount='500', category='rent')
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'لا يكفي')
        self.assertFalse(PrintTransaction.objects.exists())

    def test_collect_requires_a_customer_or_order_and_lists_open_orders_only(self):
        open_order = self.delivered('1000')
        paid_order = self.delivered('200')
        PrintTransaction.objects.create(treasury=self.treasury, transaction_type='in', amount=D('200'),
                                        order=paid_order)
        r = self.c.get(f'{self.add_url}?mode=collect', HTTP_HOST=self.host)
        self.assertContains(r, 'تحصيل من عميل')
        self.assertContains(r, f'<option value="{open_order.pk}">')
        self.assertNotContains(r, f'<option value="{paid_order.pk}">')

        r = self.guided('collect', amount='100')
        self.assertContains(r, 'دفعة العميل لازم تتربط بطلب أو بعميل')

    # ── يوم كامل من اللوحة ──────────────────────────────────────────────
    def test_a_full_day_from_the_dashboard_keeps_the_books_right(self):
        dash = self.c.get(reverse('admin:index'), HTTP_HOST=self.host)
        self.assertContains(dash, 'id="printCyclePanel"')
        self.assertContains(dash, f'{self.add_url}?mode=expense')
        self.assertContains(dash, reverse('admin:printing_materialpurchase_add'))
        self.assertContains(dash, 'id="printStartSteps"')
        self.assertContains(dash, 'خلصت 1 من 5')            # فيه عميل بس

        # 1) رأس المال في الخزنة
        r = self.guided('income', amount='5000', category='capital', description='بداية الشغل')
        self.assertRedirects(r, reverse('admin:index'), fetch_redirect_response=False)
        # 2) فاتورة شراء ورق آجل (الشاشة بتاعتها) ثم سداد جزء للمورد
        purchase = self.credit_purchase('10', '100')
        r = self.guided('pay_supplier', purchase=purchase.pk, amount='600')
        self.assertEqual(r.status_code, 302)
        # 3) فاتورة لعميل اتسلّمت ثم تحصيل جزء منها
        order = self.delivered('1500')
        r = self.guided('collect', order=order.pk, amount='1000')
        self.assertEqual(r.status_code, 302)
        # 4) مصروف إيجار
        r = self.guided('expense', amount='800', category='rent', description='إيجار المحل')
        self.assertEqual(r.status_code, 302)

        txns = {t.category: t for t in PrintTransaction.objects.all()}
        self.assertEqual((txns['capital'].transaction_type, txns['rent'].transaction_type), ('in', 'out'))
        self.assertEqual((txns['customer'].customer_id, txns['supplier'].supplier_id),
                         (self.customer.pk, self.supplier.pk))
        order.refresh_from_db()
        purchase.refresh_from_db()
        self.treasury.refresh_from_db()
        self.assertEqual((order.paid_amount, order.remaining), (D('1000.00'), D('500.00')))
        self.assertEqual(purchase.paid_amount, D('600.00'))
        self.assertEqual(self.treasury.balance, D('4600.00'))    # 5000 − 600 + 1000 − 800

        # الدفاتر: العملاء عليهم 500، والمورد ليه 400، ورأس المال 5000
        self.assertEqual(bal('1100'), D('500.00'))
        self.assertEqual(bal('2100'), D('-400.00'))
        pnl = AccountingReportService.income_statement(self.today.replace(day=1), self.today)
        self.assertEqual(pnl['revenue']['total'], D('1500.00'))
        self.assertEqual(pnl['net_profit'], pnl['revenue']['total'] - pnl['expenses']['total'])
        self.assertIn('800.00', [str(i['amount']) for i in pnl['expenses']['items']])

        # اللوحة: أرقام الشهر = قائمة الدخل، و«ابدأ هنا» اختفت بعد ما كل الخطوات خلصت
        dash = self.c.get(reverse('admin:index'), HTTP_HOST=self.host)
        self.assertContains(dash, 'id="printMonthPnl"')
        self.assertContains(dash, f"{pnl['net_profit']:,.0f}")
        self.assertNotContains(dash, 'id="printStartSteps"')
