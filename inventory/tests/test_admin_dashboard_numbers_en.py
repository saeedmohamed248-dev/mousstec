"""
🐛 لوحة الأدمن (/secure-portal/) بالإنجليزي كانت بتغلط في الأرقام:
- رصيد الخزنة: data-value="{{ t.balance }}" ⇒ مع USE_THOUSAND_SEPARATOR بيتكتب «100,000.0»
  والـ JS بيعمل parseFloat فيقف عند الفاصلة ⇒ الكارت يقول 100.00 بدل 100,000.00.
- كارت «أرباح الشهر الصافية» كان بيعرض ربح النهارده (net_profit_today).
- عدد أوامر الشغل كان بيتقصّ من جملة «31 أوامر شغل مفتوحة» بعدد حروف ومابيتترجمش.
- نصوص اللوحة كانت عربي من غير {% trans %} ⇒ اللغة متلخبطة في الإنجليزي.
"""
import re

from django.test import Client, override_settings
from django.utils import translation

from .base import ERPTenantTestCase
from .factories import make_branch, make_employee, make_treasury


@override_settings(ALLOWED_HOSTS=['*'])
class AdminDashboardNumbersEnglishTests(ERPTenantTestCase):
    def setUp(self):
        self.branch = make_branch()
        self.user, _ = make_employee('dash_en_admin', role='admin', branch=self.branch)
        self.user.is_superuser = True
        self.user.is_staff = True
        self.user.save()
        make_treasury(self.branch, name='Big safe', balance='1234567.50')
        self.c = Client(HTTP_HOST=self.domain.domain)
        self.c.force_login(self.user)
        self.addCleanup(translation.deactivate)

    def _index(self, lang):
        self.c.cookies['django_language'] = lang
        r = self.c.get('/secure-portal/')
        self.assertEqual(r.status_code, 200)
        return r.content.decode()

    def test_treasury_balance_is_machine_readable_in_english(self):
        html = self._index('en')
        # الرقم اللي الـ JS بيقرأه لازم يكون من غير فاصلة آلاف
        self.assertIn('data-value="1234567.5', html)
        self.assertNotIn('data-value="1,234,567', html)

    def test_treasury_balance_is_machine_readable_in_arabic(self):
        self.assertIn('data-value="1234567.5', self._index('ar'))

    @staticmethod
    def _visible(html):
        """الكلام اللي بيظهر على الشاشة بس (من غير سكربت/ستايل/تعليقات/attributes)."""
        html = re.sub(r'<script.*?</script>|<style.*?</style>|<!--.*?-->', ' ', html, flags=re.S)
        return re.sub(r'<[^>]+>', ' ', html)

    def test_profit_card_says_today_not_month(self):
        self.assertIn("Today's net profit", self._index('en').replace('&#x27;', "'"))
        html = self._index('ar')
        self.assertIn('صافي ربح اليوم', html)
        self.assertNotIn('أرباح الشهر الصافية', html)

    def test_open_orders_count_and_label_are_separate_and_translated(self):
        html = self._index('en')
        self.assertIn('Open job orders', html)
        self.assertNotIn('أوامر شغل مفتوحة', html)

    def test_kpi_labels_are_translated_in_english(self):
        html = self._visible(self._index('en'))
        for ar in ('مبيعات اليوم', 'ديون متأخرة بالخارج', 'الوصول السريع', 'توريد بضاعة'):
            self.assertNotIn(ar, html, ar)
        self.assertIn('Overdue receivables', html)
