"""
⚡ الفاتورة السريعة (printing/quick_invoice.py + /printing/invoice/quick/).

🆕 فاتورة بيع في شاشة واحدة بدل فورم الأدمن (4 تابات + جدول مهام 12 عمود + تحصيل من شاشة تانية).
لازم تقيّد بالظبط زي الفاتورة المفصّلة: الإيراد لما تتسلّم، الدفعة في الخزنة ودفترها، والآجل على
العميل — والدفاتر متزنة بعد كل اختبار (tearDown).
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import connection
from django.db.models import Q, Sum
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from inventory.models import AccountingEntry
from inventory.services.accounting_reports import AccountingReportService
from inventory.tests.base import ERPTenantTestCase
from printing import ledger
from printing.models import PrintCustomer, PrintOrder, PrintTransaction, PrintTreasury
from printing.quick_invoice import create_quick_invoice

User = get_user_model()
D = Decimal


def bal(code):
    agg = (AccountingEntry.objects.filter(Q(account__code=code) | Q(account__parent__code=code))
           .aggregate(d=Sum('debit'), c=Sum('credit')))
    return (agg['d'] or D('0')) - (agg['c'] or D('0'))


@override_settings(ALLOWED_HOSTS=['*'])
class QuickInvoiceTests(ERPTenantTestCase):

    def setUp(self):
        super().setUp()
        connection.set_tenant(self.tenant)
        type(self.tenant).objects.filter(pk=self.tenant.pk).update(industry='printing')
        self.host = self.domain.domain
        self.treasury = PrintTreasury.objects.create(name='الخزنة')
        self.user = User.objects.create_superuser('quick_boss', 'q@x.com', 'x')
        self.c = Client()
        self.c.force_login(self.user)
        self.url = reverse('printing:quick_invoice')
        self.today = timezone.localdate()

    def tearDown(self):
        connection.set_tenant(self.tenant)
        tb = AccountingReportService.trial_balance()
        self.assertTrue(tb['is_balanced'], tb)
        for t in PrintTreasury.objects.all():      # كل خزنة = حسابها في الدفتر
            agg = ledger.treasury_account(t).entries.aggregate(d=Sum('debit'), c=Sum('credit'))
            self.assertEqual((agg['d'] or D('0')) - (agg['c'] or D('0')), t.balance, t.name)
        super().tearDown()

    def post(self, **data):
        payload = {'customer_name': 'شركة الندى', 'customer_phone': '01011112222', 'customer_id': '',
                   'line_desc': ['500 كارت بزنس', 'ستيكر'], 'line_qty': ['1', '10'], 'line_price': ['300', '5'],
                   'discount': '', 'delivered': '1', 'paid': '350', 'treasury': str(self.treasury.pk), 'notes': ''}
        payload.update(data)
        return self.c.post(self.url, payload, HTTP_HOST=self.host)

    # ── البيع الكاش (الأغلب) ─────────────────────────────────────────────
    def test_cash_sale_delivered_and_paid_in_one_screen(self):
        r = self.post()
        order = PrintOrder.objects.get()
        self.assertRedirects(r, reverse('printing:order_invoice', args=[order.pk]) + '?done=1',
                             fetch_redirect_response=False)
        self.assertEqual(order.status, 'delivered')
        self.assertEqual((order.total_amount, order.paid_amount, order.remaining), (D('350.00'), D('350.00'), D('0.00')))
        self.assertEqual(order.jobs.count(), 2)
        self.assertTrue(all(j.proof_waived for j in order.jobs.all()))
        self.assertEqual(PrintCustomer.objects.get().phone, '01011112222')    # عميل جديد اتسجّل
        txn = PrintTransaction.objects.get()
        self.assertEqual((txn.transaction_type, txn.category, txn.order_id, txn.customer_id, txn.amount),
                         ('in', 'customer', order.pk, order.customer_id, D('350.00')))
        self.treasury.refresh_from_db()
        self.assertEqual(self.treasury.balance, D('350.00'))
        # الإيراد في قائمة الدخل = اللي اتسلّم، والعميل مش عليه حاجة
        pnl = AccountingReportService.income_statement(self.today.replace(day=1), self.today)
        self.assertEqual(pnl['revenue']['total'], D('350.00'))
        self.assertEqual(bal('1100'), D('0.00'))

    def test_partial_payment_leaves_the_rest_on_the_customer(self):
        self.post(paid='100', discount='50')
        order = PrintOrder.objects.get()
        self.assertEqual((order.net_total, order.paid_amount, order.remaining), (D('300.00'), D('100.00'), D('200.00')))
        self.assertEqual(bal('1100'), D('200.00'))                            # العملاء عليهم 200

    def test_not_delivered_is_confirmed_and_no_revenue_yet(self):
        self.post(delivered='0', paid='100')
        order = PrintOrder.objects.get()
        self.assertEqual(order.status, 'confirmed')
        pnl = AccountingReportService.income_statement(self.today.replace(day=1), self.today)
        self.assertEqual(pnl['revenue']['total'], D('0'))                    # عربون — مش إيراد لسه
        self.treasury.refresh_from_db()
        self.assertEqual(self.treasury.balance, D('100.00'))

    def test_unpaid_needs_no_treasury(self):
        self.post(paid='0', treasury='')
        self.assertEqual(PrintOrder.objects.get().remaining, D('350.00'))
        self.assertFalse(PrintTransaction.objects.exists())

    # ── العميل ──────────────────────────────────────────────────────────
    def test_existing_customer_is_reused_by_id_phone_or_exact_name(self):
        nada = PrintCustomer.objects.create(name='شركة الندى', phone='01011112222')
        self.post(customer_id=str(nada.pk), customer_phone='')
        self.post(customer_name='اسم تاني')                                   # نفس الموبايل
        self.post(customer_phone='')                                          # نفس الاسم من غير موبايل
        self.assertEqual(PrintCustomer.objects.count(), 1)
        self.assertEqual(PrintOrder.objects.filter(customer=nada).count(), 3)

    def test_customer_name_is_required(self):
        r = self.post(customer_name='', customer_phone='')
        self.assertContains(r, 'اكتب اسم العميل')
        self.assertFalse(PrintOrder.objects.exists())

    # ── الغلط مابيسيبش نص فاتورة ─────────────────────────────────────────
    def test_bad_input_saves_nothing_and_keeps_the_form(self):
        for data, msg in (
            ({'paid': '9999'}, 'المدفوع أكبر من قيمة الفاتورة'),
            ({'discount': '9999'}, 'الخصم أكبر من إجمالي البنود'),
            ({'line_desc': ['', ''], 'line_price': ['', '']}, 'ضيف بند واحد على الأقل'),
            ({'line_qty': ['0', '1']}, 'الكمية لازم تكون 1 أو أكتر'),
            ({'line_price': ['-5', '5']}, 'لازم يكون صفر أو أكتر'),
            ({'line_desc': ['', 'ستيكر'], 'line_price': ['300', '5']}, 'اكتب وصفه'),
        ):
            r = self.post(**data)
            self.assertEqual(r.status_code, 200, data)
            self.assertContains(r, msg)
            self.assertContains(r, 'value="شركة الندى"')                     # الفورم مااتمسحش
        self.assertFalse(PrintOrder.objects.exists())
        self.assertFalse(PrintCustomer.objects.exists())                      # ولا عميل نص-متسجّل
        self.assertFalse(PrintTransaction.objects.exists())

    def test_credit_hold_keeps_it_draft_and_undelivered(self):
        PrintCustomer.objects.create(name='شركة الندى', phone='01011112222', credit_limit=D('100'))
        order, warnings = create_quick_invoice(
            user=self.user, lines=[('بنر', '1', '500')], customer_phone='01011112222', paid='0')
        self.assertEqual(order.status, 'draft')
        self.assertTrue(any('حد الائتمان' in w for w in warnings))

    def test_paid_without_treasury_is_rejected(self):
        with self.assertRaises(ValidationError):
            create_quick_invoice(user=self.user, lines=[('بنر', '1', '500')], customer_name='س', paid='100')
        self.assertFalse(PrintOrder.objects.exists())

    # ── الشاشة والصلاحيات ───────────────────────────────────────────────
    def test_page_renders_with_treasury_and_customers(self):
        PrintCustomer.objects.create(name='مكتبة النور', phone='01033334444')
        r = self.c.get(self.url, HTTP_HOST=self.host)
        self.assertContains(r, 'id="quickInvoice"')
        self.assertContains(r, f'<option value="{self.treasury.pk}"')
        self.assertContains(r, 'مكتبة النور')

    def test_needs_add_order_permission(self):
        clerk = User.objects.create_user('clerk', 'c@x.com', 'x', is_staff=True)
        c = Client()
        c.force_login(clerk)
        self.assertEqual(c.get(self.url, HTTP_HOST=self.host).status_code, 403)

    def test_invoice_page_after_save_offers_next_steps(self):
        self.post()
        order = PrintOrder.objects.get()
        r = self.c.get(reverse('printing:order_invoice', args=[order.pk]) + '?done=1', HTTP_HOST=self.host)
        self.assertContains(r, 'اتحفظت الفاتورة')
        self.assertContains(r, 'https://wa.me/201011112222?text=')
        self.assertContains(r, reverse('printing:quick_invoice'))

    def test_dashboard_points_to_quick_invoice(self):
        dash = self.c.get(reverse('admin:index'), HTTP_HOST=self.host)
        self.assertContains(dash, f'<a class="pc-btn pc-main" href="{self.url}">')
        self.assertContains(dash, 'id="printMoreDetails"')                    # الأقسام القديمة مقفولة

    # ── الدورة كاملة: تسليم بعدين، مرتجع، والإنتاج ────────────────────────
    def test_delivered_now_closes_the_jobs_for_production(self):
        self.post()
        order = PrintOrder.objects.get()
        self.assertTrue(all(j.is_complete and j.stage == 'done' for j in order.jobs.all()))

    def test_in_progress_then_delivered_from_the_invoice_page(self):
        self.post(delivered='0', paid='100')
        order = PrintOrder.objects.get()
        self.assertEqual(order.status, 'confirmed')
        self.assertFalse(order.jobs.filter(is_complete=True).exists())        # شغل جاري في الإنتاج
        page = reverse('printing:order_invoice', args=[order.pk])
        deliver = reverse('printing:order_deliver', args=[order.pk])
        self.assertContains(self.c.get(page, HTTP_HOST=self.host), f'action="{deliver}"')

        r = self.c.post(deliver, HTTP_HOST=self.host)
        self.assertRedirects(r, page, fetch_redirect_response=False)
        order.refresh_from_db()
        self.assertEqual(order.status, 'delivered')
        self.assertTrue(all(j.is_complete for j in order.jobs.all()))
        pnl = AccountingReportService.income_statement(self.today.replace(day=1), self.today)
        self.assertEqual(pnl['revenue']['total'], D('350.00'))
        self.assertEqual(bal('1100'), D('250.00'))                            # الباقي على العميل
        page_html = self.c.get(page, HTTP_HOST=self.host).content.decode()
        self.assertIn('الفاتورة اتسلّمت', page_html)
        self.assertNotIn(f'action="{deliver}"', page_html)                    # مابيتسلّمش مرتين
        self.assertIn(reverse('admin:printing_printcreditnote_add') + f'?order={order.pk}', page_html)  # مرتجع
        self.assertIn(f"{reverse('printing:quick_collect')}?customer={order.customer_id}&amp;order={order.pk}", page_html)
        # ومرة تانية بالـ POST = رسالة، مش خطأ
        self.c.post(deliver, HTTP_HOST=self.host)
        self.assertIn('مش في حالة تتسلّم', self.c.get(page, HTTP_HOST=self.host).content.decode())

    def test_deliver_refuses_a_job_waiting_for_proof(self):
        from printing.models import PrintJob
        order, _w = create_quick_invoice(user=self.user, lines=[('بنر', '1', '500')], customer_name='س',
                                         delivered=False)
        PrintJob.objects.create(order=order, description='تصميم لوجو', quantity=1, unit_price=D('100'))  # بروفة لازمة
        r = self.c.post(reverse('printing:order_deliver', args=[order.pk]), HTTP_HOST=self.host, follow=True)
        self.assertContains(r, 'مستني موافقة العميل على البروفة')
        order.refresh_from_db()
        self.assertEqual(order.status, 'confirmed')

    def test_deliver_needs_change_permission(self):
        order, _w = create_quick_invoice(user=self.user, lines=[('بنر', '1', '500')], customer_name='س',
                                         delivered=False)
        clerk = User.objects.create_user('viewer', 'v@x.com', 'x', is_staff=True)
        c = Client()
        c.force_login(clerk)
        self.assertEqual(c.post(reverse('printing:order_deliver', args=[order.pk]), HTTP_HOST=self.host).status_code, 403)
        order.refresh_from_db()
        self.assertEqual(order.status, 'confirmed')
