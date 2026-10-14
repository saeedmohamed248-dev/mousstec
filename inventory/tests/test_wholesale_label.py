"""
🏷️ صفحة الأصناف بشكل ERP عالمي: التكلفة (شراء · مصاريف) · سعر البيع (جملة لو فيه) ·
الربح للقطعة والهامش %.

«جملة» تحت سعر البيع كان فيها سعر الشراء: رفع فاتورة شراء من ملف عموده الوحيد
«سعر الجملة» (= سعر الشراء عندنا) كان بيحفظه كمان سعر بيع جملة للصنف — فالبيع
للتجار بيطلع بسعر الشراء. الرفع اتصلّح، والأرقام القديمة بتتمسح (migration 0063).
"""
import importlib
import json
from decimal import Decimal

from django.apps import apps as global_apps
from django.contrib.auth.models import User
from django.db import connection
from django.test import Client, override_settings

from .base import ERPTenantTestCase
from .factories import make_branch, make_product, make_treasury, make_vendor


@override_settings(ALLOWED_HOSTS=['*'])
class ProductPricesTests(ERPTenantTestCase):
    def setUp(self):
        connection.set_tenant(self.tenant)
        self.host = self.domain.domain
        self.branch = make_branch()
        make_treasury(self.branch)
        self.c = Client()
        self.c.force_login(User.objects.create_user('wl_boss', password='x', is_staff=True, is_superuser=True))

    def buy(self, items, shipping=0):
        body = {'vendor_id': make_vendor().pk, 'branch_id': self.branch.pk, 'treasury_id': None, 'paid_amount': 0,
                'items': items,
                'extra_costs': [{'kind': 'shipping', 'behavior': 'landed', 'amount': shipping}] if shipping else []}
        r = self.c.post('/system/purchases/save/', json.dumps(body), content_type='application/json',
                        HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 200, r.content)

    def test_cost_sale_profit_and_margin_columns(self):
        part = make_product(part_number='WL-1', name='اسكترا e60', retail_price='7500',
                            purchase_price='0', average_cost='0', b2b_wholesale_price=Decimal('6900'))
        self.buy([{'product_id': part.pk, 'qty': 1, 'cost': 4400}], shipping=2000)
        html = self.c.get('/system/products/', HTTP_HOST=self.host).content.decode()
        self.assertIn('شراء 4,400.00 · مصاريف 2,000.00', html)     # التكلفة 6,400
        self.assertIn('جملة 6,900.00', html)
        self.assertIn('1,100.00', html)                              # ربح القطعة 7,500 − 6,400
        self.assertIn('14.7%', html)                                 # الهامش من سعر البيع
        self.assertNotIn('مش فوق', html)

    def test_migration_clears_wholesale_that_was_the_purchase_price(self):
        bad = make_product(part_number='WL-2', name='طابلوه g05', retail_price='60000', purchase_price='35000',
                           average_cost='35000', b2b_wholesale_price=Decimal('30000'))
        same = make_product(part_number='WL-3', name='باكته', retail_price='600', purchase_price='425',
                            average_cost='425', b2b_wholesale_price=Decimal('425'))
        good = make_product(part_number='WL-4', name='فلتر', retail_price='300', purchase_price='100',
                            average_cost='100', b2b_wholesale_price=Decimal('250'))
        mig = importlib.import_module('inventory.migrations.0063_clear_wholesale_that_was_purchase_price')
        mig.clear(global_apps, None)
        for p in (bad, same, good):
            p.refresh_from_db()
        self.assertEqual((bad.b2b_wholesale_price, same.b2b_wholesale_price, good.b2b_wholesale_price),
                         (Decimal('0.00'), Decimal('0.00'), Decimal('250.00')))

    def test_import_does_not_store_the_purchase_price_as_our_wholesale_price(self):
        from inventory.services.purchase_import import rows_to_items
        # ملف المورد: «سعر الجملة» بتاعه = سعر الشراء بتاعنا (مفيش عمود تكلفة)
        items = rows_to_items([['كود', 'الصنف', 'الكمية', 'سعر الجملة', 'سعر البيع'],
                               ['E60-1', 'اسكترا e60', '1', '4400', '7500']])
        self.assertEqual((items[0]['cost'], items[0]['wholesale'], items[0]['sell']), (4400.0, 0, 7500.0))
        self.assertEqual(items[0]['cost_guess'], 'wholesale')
