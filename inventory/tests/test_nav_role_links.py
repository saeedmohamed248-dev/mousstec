"""
🧭 القائمة مابتعرضش لينكات هتطلع للموظف 403 (دورات ناقصة لقيناها في جولة الموبايل).
الكاشير/الفني كانوا بيشوفوا «الجرد الفعلي» و«الاستيراد» و«استوديو الصور» ويدوسوا يقعوا على صفحة ممنوع.
"""
from django.contrib.auth.models import User
from django.db import connection
from django.test import Client, override_settings
from django.urls import reverse

from .base import ERPTenantTestCase
from .factories import make_branch, make_employee

GATED = ('stock_count', 'product_import', 'image_studio', 'inventory_import')


@override_settings(ALLOWED_HOSTS=['*'])
class NavRoleLinksTests(ERPTenantTestCase):
    def setUp(self):
        connection.set_tenant(self.tenant)
        self.host = self.domain.domain
        self.branch = make_branch()

    def _dashboard(self, user):
        c = Client()
        c.force_login(user)
        r = c.get(reverse('inventory:product_list'), HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 200)
        return r.content.decode()

    def test_cashier_does_not_see_forbidden_stock_links(self):
        user, _ = make_employee('nav_cashier', role='cashier', branch=self.branch)
        html = self._dashboard(user)
        for name in GATED:
            self.assertNotIn(f'href="{reverse("inventory:" + name)}"', html, name)
            r = Client()
            r.force_login(user)
            # والصفحة نفسها فعلاً ممنوعة عليه — يعني اللينك كان دورة مقفولة
            self.assertEqual(r.get(reverse('inventory:' + name), HTTP_HOST=self.host).status_code, 403, name)

    def test_superuser_sees_all_stock_links(self):
        boss = User.objects.create_user('nav_boss', password='x', is_staff=True, is_superuser=True)
        html = self._dashboard(boss)
        for name in GATED + ('stock_transfer_list', 'serials'):
            self.assertIn(f'href="{reverse("inventory:" + name)}"', html, name)
