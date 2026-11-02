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
