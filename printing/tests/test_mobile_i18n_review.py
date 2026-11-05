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
        self.assertNotIn("created at", ar)            # verbose_name على الموديل (مهاجرة 0011)
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


# ─────────────────────────────────────────────────────────────────────
# الجولة التانية: التقارير، كشف الحساب، تغيير كلمة السر، التحقق بخطوتين، 404 القطاعات
# ─────────────────────────────────────────────────────────────────────
@override_settings(ALLOWED_HOSTS=['*'])
class PrintingI18nRound2Tests(PrintingI18nTests):
    # بيورث setUpClass/setUp/_get بس — الاختبارات اللي فوق مش بتتكرر
    test_print_dashboard_in_english_has_no_arabic_labels = None
    test_breadcrumb_app_name_and_footer = None
    test_created_at_filter_is_labelled = None
    test_live_sync_badge_is_translated = None

    def test_reports_page_in_english(self):
        html = self._get('/secure-portal/reports/', 'en')
        for en in ('Reports & analytics', 'Back to dashboard', "Today's sales", 'Debt aging',
                   'No data for this period'):
            self.assertIn(en, html)
        for ar in ('التقارير والتحليلات', 'مبيعات اليوم', 'تقادم الديون', 'لا توجد بيانات للفترة المحددة'):
            self.assertNotIn(ar, html)

    def test_account_security_pages_follow_the_language(self):
        for path, en in (('/account/change-password/', 'Save new password'),
                         ('/account/mfa/', 'An extra layer of protection for your account')):
            html = self._get(path, 'en')
            self.assertIn('dir="ltr"', html)
            self.assertIn(en, html)
            self.assertNotIn('© 2026 Mouss Tec Ecosystem', html)
            self.assertIn('dir="rtl"', self._get(path, 'ar'))

    def test_change_password_user_label_is_not_used(self):
        # «المستخدم:» في صفحة التطبيق الذكي معناها «Used:» — هنا معناها الحساب
        html = self._get('/account/change-password/', 'en')
        self.assertIn('User:', html)
        self.assertNotIn('Used:', html)

    def test_industry_404_speaks_english(self):
        c = HttpClient()
        c.force_login(self.user)
        c.cookies['django_language'] = 'en'
        r = c.get('/system/dashboard/', HTTP_HOST=self.domain.domain)   # الورشة مقفولة على المطابع
        self.assertEqual(r.status_code, 404)
        self.assertIn("This section isn&#x27;t available", r.content.decode().replace("'", '&#x27;'))

    def test_customer_statement_topbar_wraps_on_phones(self):
        tpl = (Path(settings.BASE_DIR) / 'printing/templates/printing/customer_statement.html').read_text(encoding='utf-8')
        self.assertIn('.topbar-actions { flex-wrap: wrap', tpl)


class SuperAdminMobileTests(ERPTenantTestCase):
    """📱 لوحة المالك على الموبايل: ٢٠ صفحة كانت من غير viewport (الموبايل بيرسمها بعرض ٩٨٠ ويصغّرها،
    والعربي بيبان مقصوص). الـ viewport وحماية الموبايل في _admin_theme.html اللي كل الصفحات بتضمّنه."""

    def test_every_superadmin_page_gets_a_viewport(self):
        root = Path(settings.BASE_DIR)
        theme = (root / 'clients/templates/clients/saas_admin/_admin_theme.html').read_text(encoding='utf-8')
        self.assertIn('name="viewport"', theme)
        self.assertNotIn('{# 📱', theme)          # تعليق متعدد السطور بـ {# #} بيطلع نص في الصفحة
        pages = list((root / 'clients/templates/clients/saas_admin').glob('*.html')) + [
            root / 'clients/templates/clients/super_admin_gift_diagnostics.html',
            root / 'omnichannel/templates/omnichannel/saas_admin/subscription_list.html']
        missing = [p.name for p in pages if not p.name.startswith('_')
                   and '<head' in (t := p.read_text(encoding='utf-8'))
                   and 'name="viewport"' not in t and '_admin_theme.html' not in t]
        self.assertEqual(missing, [])

    def test_wide_table_is_wrapped_even_when_it_stretches_the_page(self):
        """🐛 سجل الأخطاء وفيه صفوف: الجدول (~800 بكسل) بيوسّع الصفحة قبل السكريبت، فـ innerWidth بيبقى
        > 767 والجدول مابيتلفّش ولا الـ media query بتشتغل — الصفحة كانت فاضلة أعرض من الشاشة."""
        theme = (Path(settings.BASE_DIR) / 'clients/templates/clients/saas_admin/_admin_theme.html').read_text(encoding='utf-8')
        self.assertIn('screen.width', theme)                                   # عرض الجهاز مش عرض الصفحة
        self.assertLess(theme.index('.mt-table-scroll {'), theme.index('@media (max-width: 767px)'))  # برّه الـ media
