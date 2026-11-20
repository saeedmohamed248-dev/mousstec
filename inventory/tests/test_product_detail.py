"""🔎 صفحة القطعة: أي بند في قائمة القطع بيفتحها — الصور، الحالة، الأسعار، الرصيد، والحركات."""
from django.contrib.auth.models import User
from django.db import connection
from django.test import Client, override_settings

from inventory.models import InventoryMovement
from .base import ERPTenantTestCase
from .factories import make_branch, make_employee, make_inventory, make_product


@override_settings(ALLOWED_HOSTS=['*'])
class ProductDetailTests(ERPTenantTestCase):
    def setUp(self):
        connection.set_tenant(self.tenant)
        self.host = self.domain.domain
        self.branch = make_branch()
        self.p = make_product(part_number='PD-1', name='طقم ديانات F30', retail_price='1500', purchase_price='300',
                              average_cost='300')
        self.p.additional_part_numbers = ['PD-1B']
        self.p.description = 'طقم كامل أصلي'
        self.p.save()
        make_inventory(self.p, self.branch, quantity=4)
        InventoryMovement.objects.create(product=self.p, branch=self.branch, reason='purchase', quantity_change=4,
                                         quantity_before=0, quantity_after=4, note='توريد أول')
        self.boss, _ = User.objects.get_or_create(username='pd_boss', defaults=dict(is_staff=True, is_superuser=True))
        self.c = Client()
        self.c.force_login(self.boss)

    def test_list_rows_open_the_detail_page_with_everything(self):
        lst = self.c.get('/system/products/', HTTP_HOST=self.host).content.decode()
        self.assertIn(f'data-href="/system/products/{self.p.id}/"', lst)
        self.assertIn(f'href="/system/products/{self.p.id}/"', lst)
        r = self.c.get(f'/system/products/{self.p.id}/', HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 200)
        for text in ('طقم ديانات F30', 'PD-1B', 'طقم كامل أصلي', self.p.get_condition_display(),
                     self.branch.name, 'توريد أول', '1,500.00', '300.00', '+4'):
            self.assertContains(r, text)

    def test_cost_hidden_for_employee_without_cost_view_and_missing_product_redirects(self):
        user, prof = make_employee(username='pd_cashier', role='cashier', branch=self.branch)
        prof.can_see_costs = False
        prof.save()
        c = Client()
        c.force_login(user)
        r = c.get(f'/system/products/{self.p.id}/', HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.context['show_cost'])
        self.assertNotContains(r, 'التكلفة (متوسط)')
        r = self.c.get('/system/products/999999/', HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 302)
