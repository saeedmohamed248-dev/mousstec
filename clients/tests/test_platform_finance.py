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
        self.assertEqual(k['burn'], Decimal('100.00'))                         # ٣٠٠ في آخر ٩٠ يوم ÷ ٣
        self.assertIsNone(k['runway_months'])                                  # الإيراد أكبر ⇒ مفيش حرق
        self.assertEqual((k['mrr'], k['active_customers'], k['churn'], k['cac']), (Decimal('0.00'), 0, None, None))


class AccrualBasisTests(TestCase):
    """🌍 أساس الاستحقاق زي الشركات العالمية: المبلغ اللي ليه فترة بيتوزّع على أيامها."""

    def test_prelaunch_expense_spread_over_its_period_but_cash_on_payment_date(self):
        # مصاريف فترة الإطلاق ٣٠٠٠ اندفعت ١ نوفمبر وبتغطي سبتمبر→نوفمبر (٩١ يوم)
        e = _exp(D(2026, 11, 1), '9100', 'prelaunch', period_start=D(2026, 9, 1), period_end=D(2026, 11, 30))
        self.assertTrue(e.is_spread)
        sep = pf.expenses(D(2026, 9, 1), D(2026, 9, 30))['total']
        octo = pf.expenses(D(2026, 10, 1), D(2026, 10, 31))['total']
        nov = pf.expenses(D(2026, 11, 1), D(2026, 11, 30))['total']
        self.assertEqual((sep, octo, nov), (Decimal('3000.00'), Decimal('3100.00'), Decimal('3000.00')))
        self.assertEqual(sep + octo + nov, Decimal('9100.00'))                 # مفيش قرش بيضيع
        cash_sep = pf.expenses(D(2026, 9, 1), D(2026, 9, 30), pf.CASH)['total']
        cash_nov = pf.expenses(D(2026, 11, 1), D(2026, 11, 30), pf.CASH)['total']
        self.assertEqual((cash_sep, cash_nov), (Decimal('0'), Decimal('9100.00')))

    def test_prepaid_balance_and_reversed_or_half_period_is_cleaned(self):
        _exp(D(2026, 11, 1), '1200', 'hosting', period_start=D(2026, 11, 1), period_end=D(2027, 10, 31))
        b = pf.balance_items(D(2026, 11, 30))
        self.assertEqual(b['prepaid_expenses'], Decimal('1101.37'))           # ٣٣٥ يوم من ٣٦٥ لسه
        half = _exp(D(2026, 11, 1), '10', period_start=D(2026, 11, 1))          # بداية من غير نهاية ⇒ مفيش فترة
        self.assertEqual((half.period_start, half.period_end), (None, None))
        rev = _exp(D(2026, 11, 1), '10', period_start=D(2026, 12, 31), period_end=D(2026, 12, 1))
        self.assertEqual((rev.period_start, rev.period_end), (D(2026, 12, 1), D(2026, 12, 31)))

    def test_recurring_until_stops_copies(self):
        src = _exp(D(2026, 6, 10), '100', is_recurring=True, recurring_until=D(2026, 8, 31))
        self.assertEqual(pf.generate_recurring(D(2026, 11, 20)), 2)            # يوليو وأغسطس بس
        self.assertEqual(sorted(src.copies.values_list('date', flat=True)), [D(2026, 7, 10), D(2026, 8, 10)])


class SubscriptionAccrualAndUnitEconomicsTests(_PublicDomainMixin, TestCase):
    """اشتراك سنوي مدفوع مقدّم = إيراد ١٢ شهر (IFRS 15)، والباقي إيراد مؤجّل؛ MRR وChurn وLTV."""

    def setUp(self):
        import datetime as _dt
        from django.utils import timezone
        from clients.models import Client, Plan, PlatformInvoice, TenantSubscription
        from clients.services import platform_billing as pb
        plan = Plan.objects.update_or_create(slug='auto-silver', defaults=dict(
            name='Silver', industry='automotive', monthly_price=Decimal('500'), is_active=True))[0]
        rev = pb._revision_for(plan)

        def shop(schema):
            t = Client(name=schema, owner_name='x', phone='0100', schema_name=schema, is_active=True)
            t.auto_create_schema = False
            t.save()
            return t, TenantSubscription.objects.create(tenant=t, plan=plan)

        def invoice(t, sub, start, end, months, total, paid):
            PlatformInvoice.objects.create(
                tenant=t, subscription=sub, plan_revision=rev, period_start=start, period_end=end,
                billing_cycle_months=months, subtotal=total, total=total, status='paid',
                paid_at=timezone.make_aware(_dt.datetime.combine(paid, _dt.time(12))))

        a, sa = shop('fin_a')       # سنوي ٦٠٠٠ اتدفع ١ نوفمبر
        invoice(a, sa, D(2026, 11, 1), D(2027, 10, 31), 12, Decimal('6000'), D(2026, 11, 1))
        b, sb = shop('fin_b')       # شهري ٥٠٠ وماجدّدش ⇒ churn
        invoice(b, sb, D(2026, 10, 1), D(2026, 10, 31), 1, Decimal('500'), D(2026, 10, 1))
        _exp(D(2026, 11, 2), '600', 'ads')

    def test_annual_plan_recognised_monthly_with_deferred_balance(self):
        nov = pf.revenue(D(2026, 11, 1), D(2026, 11, 30))['subscriptions']
        self.assertEqual(nov, Decimal('493.15'))                               # ٣٠ يوم من ٣٦٥
        self.assertEqual(pf.revenue(D(2026, 11, 1), D(2026, 11, 30), pf.CASH)['subscriptions'], Decimal('6000'))
        self.assertEqual(pf.balance_items(D(2026, 11, 30))['deferred_revenue'], Decimal('5506.85'))

    def test_mrr_churn_arpa_cac(self):
        k = pf.kpis(D(2026, 11, 20))
        self.assertEqual(k['mrr'], Decimal('500.00'))                          # ٦٠٠٠ ÷ ١٢
        self.assertEqual((k['active_customers'], k['arpa']), (1, Decimal('500.00')))
        self.assertEqual((k['churn'], k['lost_customers']), (100.0, 1))       # b كان شغّال من ٣٠ يوم وماجدّدش
        self.assertEqual(k['new_customers'], 2)
        self.assertEqual(k['cac'], Decimal('300.00'))                          # ٦٠٠ تسويق ÷ ٢ عميل جديد


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
        r = self.c.post('/superadmin/finance/expense/add/', {
            'date': today.isoformat(), 'category': 'prelaunch', 'description': 'مصاريف الإطلاق', 'amount': '6000',
            'currency': 'EGP', 'period_start': '2026-01-01', 'period_end': '2026-06-30'})
        self.assertEqual(PlatformExpense.objects.get(description='مصاريف الإطلاق').period_end, D(2026, 6, 30))
        page = self.c.get('/superadmin/finance/?period=custom&from=2026-01-01&to=2026-01-31')
        self.assertContains(page, 'مصاريف الإطلاق')                            # اتقسّمت على يناير
        self.assertContains(page, '1,027.62')                                  # ٦٠٠٠ × ٣١ ÷ ١٨١
        self.assertEqual(self.c.get('/superadmin/finance/?basis=cash').status_code, 200)
        r = self.c.post('/superadmin/finance/expense/add/', {
            'date': today.isoformat(), 'category': 'ads', 'description': 'y', 'amount': '5', 'currency': 'EGP',
            'period_start': '2026-01-01'}, follow=True)
        self.assertContains(r, 'بداية ونهاية الفترة')
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
