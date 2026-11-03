"""
📱🌐 مراجعة المطابع على الموبايل + الترجمة عربي/إنجليزي.

* لوحة تحكم المطبعة بالإنجليزي كانت ~٤٠ جملة عربي (الترحيب، الكروت، الرادار، الخزائن…)،
  واسم القسم «🎨 إدارة المطابع والتصميم» عربي في مسار التنقّل بكل الصفحات.
* بالعربي: «Dashboard» في القائمة، «Live Sync Active»، فلتر «created at»، وفوتر
  «Copyright © 2026 © 2026 Mouss Tec Ecosystem» (السنة مكررة).
* الموبايل: القائمة الجانبية في العربي بتطلع من الشمال، الفورم محشور في ~180 بكسل،
  وتقرير الأرباح أعرض من الشاشة — اتصلّحوا في CSS (اتجرّبوا في Chromium بمقاس آيفون).
"""
import re
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client as HttpClient, override_settings

from inventory.tests.base import ERPTenantTestCase
from printing.models import PrintCustomer

User = get_user_model()
_AR = re.compile(r'[؀-ۿ]')


@override_settings(ALLOWED_HOSTS=['*'])
class PrintingI18nTests(ERPTenantTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        connection.set_schema_to_public()
        type(cls.tenant).objects.filter(pk=cls.tenant.pk).update(industry='printing')
        cls.tenant.refresh_from_db()
        connection.set_tenant(cls.tenant)

    def setUp(self):
        connection.set_tenant(self.tenant)
        self.user = User.objects.create_superuser('print_i18n', 'p@x.com', 'x')
        PrintCustomer.objects.create(name='عميل', phone='01011112222')

    def _get(self, path, lang):
        c = HttpClient()
        c.force_login(self.user)
        c.cookies['django_language'] = lang
        r = c.get(path, HTTP_HOST=self.domain.domain)
        self.assertEqual(r.status_code, 200, path)
        return r.content.decode()

    def test_print_dashboard_in_english_has_no_arabic_labels(self):
        html = self._get('/secure-portal/', 'en')
        for en in ('Welcome to the print shop control center', 'Print shop revenue analysis',
                   'Smart print-shop radar', 'Orders in progress', 'Customer receivables', 'Low materials'):
            self.assertIn(en, html)
        for ar in ('مرحباً بك في', 'تحليل إيرادات المطبعة', 'رادار المطبعة الذكي', 'طلبات قيد التنفيذ',
                   'مستحقات العملاء', 'خامات منخفضة', 'متصل بالسحابة', '> الخزائن والأرصدة المالية'):
            self.assertNotIn(ar, html)

    def test_breadcrumb_app_name_and_footer(self):
        en = self._get('/secure-portal/printing/printorder/', 'en')
        self.assertIn('Print &amp; design management', en)
        self.assertNotIn('إدارة المطابع والتصميم', en)
        self.assertNotIn('© 2026 © 2026', en)
        ar = self._get('/secure-portal/printing/printorder/', 'ar')
        self.assertIn('إدارة المطابع والتصميم', ar)
        self.assertIn('لوحة التحكم', ar)                  # كانت «Dashboard»
        self.assertIn('كل الحقوق محفوظة', ar)

    def test_created_at_filter_is_labelled(self):
        ar = self._get('/secure-portal/printing/printcustomer/', 'ar')
        self.assertIn('تاريخ الإضافة', ar)
        self.assertNotIn('created at', ar)
        self.assertIn('Date added', self._get('/secure-portal/printing/printcustomer/', 'en'))

    def test_live_sync_badge_is_translated(self):
        ar = self._get('/secure-portal/', 'ar')
        self.assertNotIn('Live Sync Active', ar)
        self.assertIn('تحديث مباشر', ar)


class PrintingMobileCssTests(ERPTenantTestCase):
    """الـ CSS نفسه اتجرّب بالعين في Chromium (iPhone 13) — هنا بنتأكد إن القواعد ماتتشالش بالغلط."""

    def test_admin_mobile_rules_present(self):
        css = (Path(settings.BASE_DIR) / 'static/admin/mousstec_nav.css').read_text(encoding='utf-8')
        self.assertIn('html[dir="rtl"] body:not(.sidebar-open) .main-sidebar', css)   # القائمة من اليمين
        self.assertIn('.content-wrapper .card-body.p-5', css)                         # حشو الفورم
        self.assertIn('.main-header form.form-inline { display: none', css)           # المربع الفاضي

    def test_profit_loss_filter_wraps_on_phones(self):
        tpl = (Path(settings.BASE_DIR) / 'printing/templates/printing/profit_loss.html').read_text(encoding='utf-8')
        self.assertIn('.period-bar form { flex-wrap: wrap', tpl)

    def test_support_button_is_compact_on_phones(self):
        js = (Path(settings.BASE_DIR) / 'static/js/support_widget.js').read_text(encoding='utf-8')
        self.assertIn('body.sidebar-open .mt-sw-fab{display:none}', js)
