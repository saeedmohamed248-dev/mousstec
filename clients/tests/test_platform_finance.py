"""📒 حسابات المنصة: مصروفات بعملات وسعر صرف، إيراد يدوي، قايمة الدخل، المتكرر، والصفحة."""
import datetime
import shutil
import tempfile
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client as HttpClient, TestCase, override_settings

from clients.models import PlatformExpense, PlatformIncome
from clients.services import platform_finance as pf
from marketplace_c2c.tests.test_parts_order_lifecycle import _PublicDomainMixin

User = get_user_model()
_MEDIA = tempfile.mkdtemp(prefix='mt_pfin_')
D = datetime.date


def _exp(date, amount, category='hosting', currency='EGP', rate='1', **kw):
    return PlatformExpense.objects.create(date=date, category=category, description='x', amount=Decimal(amount),
                                          currency=currency, exchange_rate=Decimal(rate), **kw)


class PlatformFinanceServiceTests(TestCase):
    def test_foreign_currency_is_frozen_at_payment_rate(self):
        e = _exp(D(2026, 11, 3), '20', currency='USD', rate='48.50')
        self.assertEqual(e.amount_egp, Decimal('970.00'))
        e2 = _exp(D(2026, 11, 3), '100', currency='EGP', rate='99')          # الجنيه سعره ١ دايماً
        self.assertEqual((e2.exchange_rate, e2.amount_egp), (Decimal('1'), Decimal('100.00')))

    def test_profit_and_loss_groups_cogs_and_opex_and_excludes_financing(self):
        _exp(D(2026, 11, 2), '1000', 'hosting')                                # تكلفة تشغيل
        _exp(D(2026, 11, 5), '50', 'ads', currency='USD', rate='50')           # تسويق 2500
        _exp(D(2026, 11, 6), '3000', 'salaries')                               # إدارية
        _exp(D(2026, 10, 30), '999', 'hosting')                                # برّه الفترة
        PlatformIncome.objects.create(date=D(2026, 11, 10), category='services', description='تركيب',
                                      amount=Decimal('8000'))
        PlatformIncome.objects.create(date=D(2026, 11, 11), category='investment', description='مستثمر',
                                      amount=Decimal('50000'))
        p = pf.profit_and_loss(D(2026, 11, 1), D(2026, 11, 30))
        self.assertEqual(p['revenue']['total'], Decimal('8000'))
        self.assertEqual(p['revenue']['financing'], Decimal('50000'))
        self.assertEqual(p['cogs'], Decimal('1000'))
        self.assertEqual(p['gross_profit'], Decimal('7000'))
        self.assertEqual(p['opex'], Decimal('5500'))
        self.assertEqual(p['net_profit'], Decimal('1500'))
        self.assertEqual(p['expenses']['by_category'][0]['key'], 'salaries')   # الأكبر الأول
        self.assertEqual(pf.previous_period(D(2026, 11, 1), D(2026, 11, 30)), (D(2026, 10, 2), D(2026, 10, 31)))

    def test_recurring_expense_copies_each_missed_month_once(self):
        src = _exp(D(2026, 8, 31), '30', currency='USD', rate='48', is_recurring=True)
        self.assertEqual(pf.generate_recurring(D(2026, 11, 20)), 2)            # سبتمبر وأكتوبر (نوفمبر ٣٠ لسه ماجاش)
        self.assertEqual(pf.generate_recurring(D(2026, 11, 20)), 0)            # مايتكررش
        dates = sorted(src.copies.values_list('date', flat=True))
        self.assertEqual(dates, [D(2026, 9, 30), D(2026, 10, 31)])
        self.assertTrue(all(c.amount_egp == Decimal('1440.00') for c in src.copies.all()))

    def test_monthly_series_and_kpis(self):
        _exp(D(2026, 10, 5), '300')
        PlatformIncome.objects.create(date=D(2026, 10, 7), category='services', description='x', amount=Decimal('500'))
        series = pf.monthly_series(3, D(2026, 11, 20))
        self.assertEqual([m['month'] for m in series], [D(2026, 9, 1), D(2026, 10, 1), D(2026, 11, 1)])
        self.assertEqual((series[1]['revenue'], series[1]['expenses'], series[1]['net']),
                         (Decimal('500'), Decimal('300'), Decimal('200')))
        k = pf.kpis(D(2026, 11, 20), cash_balance=Decimal('1000'))
        self.assertEqual(k['burn'], Decimal('100.00'))                         # ٣٠٠ ÷ ٣ شهور
        self.assertIsNone(k['runway_months'])                                  # الإيراد أكبر ⇒ مفيش حرق


@override_settings(ALLOWED_HOSTS=['*'], MEDIA_ROOT=_MEDIA)
class PlatformFinancePageTests(_PublicDomainMixin, TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(_MEDIA, ignore_errors=True)

    def setUp(self):
        self.owner = User.objects.create_user('fin_owner', password='x', is_superuser=True, is_staff=True)
        self.c = HttpClient()
        self.c.force_login(self.owner)

    def test_add_expense_with_dollar_and_receipt_then_page_and_export(self):
        today = datetime.date.today()
        r = self.c.post('/superadmin/finance/expense/add/', {
            'date': today.isoformat(), 'category': 'ads', 'description': 'حملة فيسبوك', 'vendor': 'Meta',
            'amount': '40', 'currency': 'USD', 'exchange_rate': '49.5', 'payment_method': 'card',
            'receipt': SimpleUploadedFile('r.png', b'\x89PNG\r\n\x1a\n' + b'0' * 32, content_type='image/png')})
        self.assertEqual(r.status_code, 302)
        e = PlatformExpense.objects.get()
        self.assertEqual((e.amount_egp, e.created_by_id), (Decimal('1980.00'), self.owner.pk))
        page = self.c.get('/superadmin/finance/')
        self.assertContains(page, 'حملة فيسبوك')
        self.assertContains(page, '1,980.00')
        self.assertContains(page, '"USD": "49.5000"')                         # آخر سعر صرف بيتملا لوحده
        self.assertEqual(self.c.get(f'/superadmin/finance/expense/{e.id}/receipt/').status_code, 200)
        self.assertEqual(HttpClient().get(f'/superadmin/finance/expense/{e.id}/receipt/').status_code, 302)
        x = self.c.get('/superadmin/finance/?export=xlsx')
        self.assertEqual(x.status_code, 200)
        self.assertIn('spreadsheet', x['Content-Type'])

    def test_validation_and_delete_and_permissions(self):
        r = self.c.post('/superadmin/finance/expense/add/', {'date': '2026-11-01', 'category': 'ads',
                                                              'description': 'x', 'amount': '10', 'currency': 'USD'},
                        follow=True)
        self.assertContains(r, 'سعر الصرف')                                    # دولار من غير سعر صرف
        self.assertFalse(PlatformExpense.objects.exists())
        self.c.post('/superadmin/finance/income/add/', {'date': '2026-11-01', 'category': 'services',
                                                        'description': 'تدريب', 'amount': '700', 'currency': 'EGP'})
        inc = PlatformIncome.objects.get()
        self.c.post(f'/superadmin/finance/income/{inc.id}/delete/')
        self.assertFalse(PlatformIncome.objects.exists())
        self.assertEqual(HttpClient().get('/superadmin/finance/').status_code, 302)
        for period in ('last_month', 'quarter', 'year', 'last_year'):
            self.assertEqual(self.c.get(f'/superadmin/finance/?period={period}').status_code, 200)
        self.assertEqual(self.c.get('/superadmin/finance/?period=custom&from=2026-12-01&to=2026-01-01').status_code, 200)
