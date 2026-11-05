"""📅 تاريخ فاتورة المطبعة.

🐛 قبل كده: التاريخ كان auto_now_add — مفيش أي طريقة تسجّل فاتورة قديمة (من الورق) بتاريخها،
ولا تصلّح تاريخ اتكتب غلط. دلوقتي:
* الفاتورة السريعة فيها خانة «تاريخ الفاتورة» (النهارده افتراضياً، مش في المستقبل).
* التاريخ بيتعدّل من الأدمن، وقيد الطلب في الدفتر بيتنقل معاه.
* فترة محاسبية مقفولة ⇒ التعديل بيترفض (رسالة على الخانة).
"""
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import connection
from django.db.models import Sum
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from inventory.models import AccountingPeriod, FiscalYear, JournalEntry
from inventory.services.accounting_reports import AccountingReportService
from inventory.tests.base import ERPTenantTestCase
from printing import ledger
from printing.models import PrintOrder, PrintTransaction, PrintTreasury

User = get_user_model()
D = Decimal


@override_settings(ALLOWED_HOSTS=['*'])
class PrintInvoiceDateTests(ERPTenantTestCase):

    def setUp(self):
        super().setUp()
        connection.set_tenant(self.tenant)
        type(self.tenant).objects.filter(pk=self.tenant.pk).update(industry='printing')
        self.host = self.domain.domain
        self.treasury = PrintTreasury.objects.create(name='الخزنة')
        self.user = User.objects.create_superuser('date_boss', 'd@x.com', 'x')
        self.c = Client()
        self.c.force_login(self.user)
        self.today = timezone.localdate()
        self.back = self.today - timedelta(days=40)

    def tearDown(self):
        connection.set_tenant(self.tenant)
        tb = AccountingReportService.trial_balance()
        self.assertTrue(tb['is_balanced'], tb)
        for t in PrintTreasury.objects.all():
            agg = ledger.treasury_account(t).entries.aggregate(d=Sum('debit'), c=Sum('credit'))
            self.assertEqual((agg['d'] or D('0')) - (agg['c'] or D('0')), t.balance, t.name)
        super().tearDown()

    def post(self, **data):
        payload = {'customer_name': 'شركة الندى', 'customer_phone': '01011112222', 'customer_id': '',
                   'line_desc': ['500 كارت'], 'line_qty': ['1'], 'line_price': ['300'],
                   'discount': '', 'delivered': '1', 'paid': '300', 'treasury': str(self.treasury.pk),
                   'notes': ''}
        payload.update(data)
        return self.c.post(reverse('printing:quick_invoice'), payload, HTTP_HOST=self.host)

    def _period(self, start, end, closed):
        fy, _ = FiscalYear.objects.get_or_create(
            code='PD', defaults={'name': 'PD', 'start_date': date(start.year - 1, 1, 1),
                                 'end_date': date(self.today.year, 12, 31)})
        return AccountingPeriod.objects.create(fiscal_year=fy, name=f'P{start}', start_date=start,
                                               end_date=end, is_closed=closed)

    def _je_day(self, order):
        return set(JournalEntry.objects.filter(reference=ledger.ref_order(order.pk)).values_list('date', flat=True))

    def test_quick_invoice_with_an_old_date(self):
        r = self.post(invoice_date=self.back.isoformat())
        self.assertEqual(r.status_code, 302, r.content.decode()[:500])
        order = PrintOrder.objects.get()
        self.assertEqual(timezone.localtime(order.date_created).date(), self.back)
        self.assertEqual(timezone.localtime(order.date_delivered).date(), self.back)
        self.assertEqual(self._je_day(order), {self.back})                       # الإيراد يومها
        txn = PrintTransaction.objects.get()
        self.assertEqual(timezone.localtime(txn.date).date(), self.back)        # الدفعة يومها
        self.assertEqual(set(JournalEntry.objects.filter(reference=ledger.ref_txn(txn.pk))
                             .values_list('date', flat=True)), {self.back})

    def test_quick_invoice_defaults_to_today_and_refuses_the_future(self):
        self.post(invoice_date='')
        self.assertEqual(timezone.localtime(PrintOrder.objects.get().date_created).date(), self.today)
        r = self.post(invoice_date=(self.today + timedelta(days=2)).isoformat())
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'المستقبل')
        self.assertEqual(PrintOrder.objects.count(), 1)

    def test_editing_the_date_moves_the_ledger_entry(self):
        self.post()
        order = PrintOrder.objects.get()
        self.assertEqual(self._je_day(order), {self.today})
        order.date_created = timezone.now() - timedelta(days=40)
        order.full_clean()
        order.save()
        order.refresh_from_db()
        # فاتورة فورية (اتسلّمت يومها) ⇒ تاريخ التسليم والإيراد اتنقلوا معاها
        self.assertEqual(timezone.localtime(order.date_delivered).date(), self.back)
        self.assertEqual(self._je_day(order), {self.back})
        # الدفعة بتفضل بيوم ما الفلوس دخلت فعلاً
        txn = PrintTransaction.objects.get()
        self.assertEqual(timezone.localtime(txn.date).date(), self.today)

    def test_closed_period_blocks_the_change(self):
        self.post()
        order = PrintOrder.objects.get()
        self._period(self.back.replace(day=1), self.back.replace(day=28), True) \
            if self.back.day <= 28 else self._period(self.back.replace(day=1), self.back, True)
        order.date_created = timezone.now() - timedelta(days=40)
        with self.assertRaises(ValidationError) as cm:
            order.full_clean()
        self.assertIn('date_created', cm.exception.message_dict)
        r = self.post(invoice_date=self.back.isoformat())
        self.assertContains(r, 'مقفولة')
