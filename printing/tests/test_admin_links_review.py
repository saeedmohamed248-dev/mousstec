"""🔗 زراير الأدمن في المطابع اللي كانت بتفتح 404 (اتلقت من زحف كل صفحات شركة مطبعة جديدة).

- صفحة القسم (/secure-portal/auth/) كانت بتورّث لوحة السيارات: «توريد بضاعة» و«سوق B2B» ⇒ 404.
- صفحة تعديل المستخدم: زرار «+» جنب الفرع كان بيفتح /secure-portal/inventory/branch/add/ ⇒ 404.
"""
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client, override_settings

from inventory.tests.base import ERPTenantTestCase

User = get_user_model()


@override_settings(ALLOWED_HOSTS=['*'])
class PrintingAdminLinksTests(ERPTenantTestCase):

    def setUp(self):
        super().setUp()
        connection.set_tenant(self.tenant)
        self.host = self.domain.domain
        type(self.tenant).objects.filter(pk=self.tenant.pk).update(industry='printing')
        self.boss = User.objects.create_superuser('print_links_owner', 'pl@x.com', 'x')
        self.c = Client()
        self.c.force_login(self.boss)

    def test_section_page_lists_its_models_without_car_buttons(self):
        r = self.c.get('/secure-portal/auth/', HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        self.assertIn('/secure-portal/auth/user/', html)
        for car_link in ('/system/purchases/new/', '/system/b2b-market/', '/system/dashboard/'):
            self.assertNotIn(car_link, html)

    def test_user_page_has_no_link_to_the_closed_branch_admin(self):
        r = self.c.get(f'/secure-portal/auth/user/{self.boss.pk}/change/', HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 200)
        self.assertNotIn('/secure-portal/inventory/branch/add/', r.content.decode())


@override_settings(ALLOWED_HOSTS=['*'])
class PrintingReviewNov2026Tests(ERPTenantTestCase):
    """زحف شركة مطبعة فيها بيانات (نوفمبر ٢٠٢٦):
    - تسوية جرد محفوظة (اللينك من «كارت الصنف») كانت بتفتح 500.
    - مربع البحث اللي فوق كان بيبحث في قطع غيار العربيات ⇒ 404.
    - صفحة تأكيد حذف مستخدم كانت فيها لينك لملف الموظف في inventory ⇒ 404.
    - «أنشئ فاتورة جديدة بهذا التصميم» كان بيفتح صفحة التسويق.
    - القايمة الجانبية الإنجليزي كانت بتعرض روابط الحسابات بالعربي."""

    def setUp(self):
        super().setUp()
        connection.set_tenant(self.tenant)
        self.host = self.domain.domain
        type(self.tenant).objects.filter(pk=self.tenant.pk).update(industry='printing')
        self.tenant.industry = 'printing'
        self.boss = User.objects.create_superuser('print_review_owner', 'pr@x.com', 'x')
        self.c = Client()
        self.c.force_login(self.boss)

    def test_saved_stock_adjustment_opens(self):
        from decimal import Decimal
        from printing.models import PrintMaterial, PrintStockAdjustment
        mat = PrintMaterial.objects.create(name='ورق', quantity=Decimal('50'), cost_per_unit=Decimal('2'))
        adj = PrintStockAdjustment.objects.create(material=mat, quantity_change=Decimal('-4'))
        r = self.c.get(f'/secure-portal/printing/printstockadjustment/{adj.pk}/change/', HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 200)

    def test_top_search_box_searches_print_orders_not_car_parts(self):
        html = self.c.get('/secure-portal/', HTTP_HOST=self.host).content.decode()
        self.assertIn('action="/secure-portal/printing/printorder/"', html)
        self.assertNotIn('action="/secure-portal/inventory/product/"', html)
        self.assertEqual(self.c.get('/secure-portal/printing/printorder/?q=x', HTTP_HOST=self.host).status_code, 200)

    def test_user_delete_page_has_no_closed_inventory_links(self):
        victim = User.objects.create_user('print_victim', 'v@x.com', 'x')
        r = self.c.get(f'/secure-portal/auth/user/{victim.pk}/delete/', HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 200)
        self.assertNotIn('/secure-portal/inventory/', r.content.decode())

    def test_new_order_from_ai_design_prefills_notes(self):
        from clients.models import AIStudioSession
        html = self.c.get('/secure-portal/', HTTP_HOST=self.host).content.decode()
        self.assertNotIn("'/printing/?session='", html)
        self.assertIn('/secure-portal/printing/printorder/add/?ai_session=', html)
        connection.set_schema_to_public()
        session = AIStudioSession.objects.create(tenant=self.tenant, raw_input='بنر',
                                                 image_url='https://cdn.test/d.png')
        connection.set_tenant(self.tenant)
        r = self.c.get(f'/secure-portal/printing/printorder/add/?ai_session={session.pk}', HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'https://cdn.test/d.png')

    def test_sidebar_custom_links_are_translated_in_english(self):
        from django.utils import translation
        with translation.override('en'):
            r = self.c.get('/secure-portal/', HTTP_HOST=self.host, HTTP_ACCEPT_LANGUAGE='en')
        html = r.content.decode()
        if 'lang="en' in html:
            self.assertIn('Income statement (accrual)', html)
            self.assertNotIn('قائمة الدخل (استحقاق)', html)


@override_settings(ALLOWED_HOSTS=['*'])
class AutomotiveSearchBoxTests(ERPTenantTestCase):
    """شركات العربيات: مربع البحث اللي فوق لسه بيبحث في الأصناف (ومفيش مربع طباعة)."""

    def test_car_company_keeps_parts_search(self):
        connection.set_tenant(self.tenant)
        boss = User.objects.create_superuser('car_search_owner', 'cs@x.com', 'x')
        c = Client()
        c.force_login(boss)
        html = c.get('/secure-portal/', HTTP_HOST=self.domain.domain).content.decode()
        self.assertIn('action="/secure-portal/inventory/product/"', html)
        self.assertNotIn('action="/secure-portal/printing/printorder/"', html)
