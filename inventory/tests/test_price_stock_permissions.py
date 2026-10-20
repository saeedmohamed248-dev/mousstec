"""🏷️📦 مين يغيّر سعر البيع ومين يظبط رصيد المخزن لصنف موجود.

الكاشير كان يقدر من «تعديل القطعة» (أو «الصور والأسعار») ينزّل سعر البيع ويبيع
بيه — نقطة البيع بتحسب أي سعر أقل من سعر البيع على حد خصمه، بس لو سعر البيع نفسه
اتغيّر حد الخصم مابيشوفش حاجة — ويغيّر رصيد الفرع (تسوية جرد من غير جرد).
دلوقتي: الأسعار للمالك/المدير/المشتريات، والرصيد للمالك/المدير/أمين المخزن،
وباقي بيانات القطعة (الاسم، الماركة، الباركود…) زي ما هي لأي حد ليه صلاحية الفرع.
"""
from decimal import Decimal

from django.db import connection
from django.test import Client, override_settings

from inventory.models import Inventory, InventoryMovement, Product
from inventory.services.inventory_service import set_branch_stock
from .base import ERPTenantTestCase
from .factories import make_branch, make_employee, make_product


@override_settings(ALLOWED_HOSTS=['*'])
class PriceAndStockEditPermissionTests(ERPTenantTestCase):

    def setUp(self):
        connection.set_tenant(self.tenant)
        self.host = self.domain.domain
        self.branch = make_branch()
        self.product = make_product(part_number='PS-1', name='فلتر', retail_price='500.00',
                                    purchase_price='300.00', average_cost='300.00')
        from django.db import transaction
        with transaction.atomic():
            set_branch_stock(self.product, self.branch, 10, unit_cost=Decimal('300.00'), opening=True)

    def client_for(self, role):
        user, _ = make_employee(f'ps_{role}', role=role, branch=self.branch)
        c = Client()
        c.force_login(user)
        return c

    def edit(self, role, **changes):
        data = {'part_number': 'PS-1', 'name': 'فلتر', 'retail_price': '500.00',
                'quantity': '10', 'orig_qty': '10', 'is_active': 'on', **changes}
        return self.client_for(role).post(f'/system/products/{self.product.pk}/edit/', data,
                                          HTTP_HOST=self.host)

    def state(self):
        p = Product.objects.get(pk=self.product.pk)
        qty = Inventory.objects.get(product=p, branch=self.branch).quantity
        return p.name, p.retail_price, qty

    def test_cashier_edits_details_but_not_price_or_stock(self):
        r = self.edit('cashier', name='فلتر زيت', retail_price='1.00', quantity='99')
        self.assertIn('ok=1', r['Location'])
        self.assertEqual(self.state(), ('فلتر زيت', Decimal('500.00'), 10))
        self.assertFalse(InventoryMovement.objects.filter(reference_type='ProductEdit').exists())

    def test_cashier_cannot_change_prices_from_the_gallery(self):
        r = self.client_for('cashier').post(f'/system/products/{self.product.pk}/prices/',
                                            {'retail_price': '1.00'}, HTTP_HOST=self.host)
        self.assertIn('err=perm', r['Location'])
        self.assertEqual(self.state()[1], Decimal('500.00'))
        page = self.client_for('sales').get(f'/system/products/{self.product.pk}/gallery/', HTTP_HOST=self.host)
        self.assertContains(page, 'name="retail_price"')
        self.assertNotContains(page, 'حفظ الأسعار')

    def test_manager_changes_both_and_stock_change_is_a_recorded_adjustment(self):
        self.edit('manager', retail_price='550.00', quantity='7')
        self.assertEqual(self.state(), ('فلتر', Decimal('550.00'), 7))
        mv = InventoryMovement.objects.get(reference_type='ProductEdit')
        self.assertEqual(mv.quantity_change, -3)

    def test_purchasing_sets_prices_stock_keeper_sets_stock(self):
        self.edit('purchasing', retail_price='520.00', quantity='99')
        self.assertEqual(self.state()[1:], (Decimal('520.00'), 10))
        self.edit('stock', retail_price='1.00', quantity='12')
        self.assertEqual(self.state()[1:], (Decimal('520.00'), 12))

    def test_edit_page_locks_the_fields_it_will_ignore(self):
        page = self.client_for('cashier').get(f'/system/products/{self.product.pk}/edit/', HTTP_HOST=self.host)
        html = page.content.decode()
        self.assertIn('name="retail_price" disabled', html)
        self.assertIn('name="quantity" disabled', html)
        page = self.client_for('manager').get(f'/system/products/{self.product.pk}/edit/', HTTP_HOST=self.host)
        self.assertNotIn('name="retail_price" disabled', page.content.decode())

    def test_stock_loading_from_photo_is_for_stock_roles(self):
        self.assertEqual(self.client_for('cashier').get('/system/inventory/import/', HTTP_HOST=self.host).status_code, 403)
        self.assertEqual(self.client_for('stock').get('/system/inventory/import/', HTTP_HOST=self.host).status_code, 200)
