"""
🔐 مراجعة الصلاحيات — كل دور اتفتحت بيه كل صفحات السيستم.

* صفحة «أسعار المنتج» كانت من غير أي فحص: «المراجع (عرض فقط)» يغيّر الأسعار،
  والحقل الغايب من الفورم كان بيتسجّل صفر (التكلفة بتتمسح).
* التكلفة كانت ظاهرة وقابلة للتعديل في صفحتي «الصور والأسعار» و«تعديل القطعة»
  لأي موظف — حتى اللي مش مسموح له يشوفها.
* صور الفاتورة: صلاحية «عرض فقط» على فرع تاني ماكانتش بتمنع الرفع/الحذف.
"""
from decimal import Decimal

from django.db import connection
from django.test import Client, override_settings

from inventory.models import Product
from .base import ERPTenantTestCase
from .factories import make_branch, make_employee, make_product


@override_settings(ALLOWED_HOSTS=['*'])
class ProductPricePermissionTests(ERPTenantTestCase):
    def setUp(self):
        connection.set_tenant(self.tenant)
        self.host = self.domain.domain
        self.branch = make_branch()
        self.product = make_product(part_number='PERM-1', retail_price='500.00', purchase_price='300.00')
        Product.objects.filter(pk=self.product.pk).update(b2b_wholesale_price=Decimal('450.00'))

    def _client(self, role):
        user, _ = make_employee(f'perm_{role}', role=role, branch=self.branch)
        c = Client()
        c.force_login(user)
        return c

    def _prices(self):
        p = Product.objects.get(pk=self.product.pk)
        return p.purchase_price, p.retail_price, p.b2b_wholesale_price

    def test_viewer_cannot_change_prices(self):
        self._client('viewer').post(f'/system/products/{self.product.pk}/prices/',
                                    {'retail_price': '1'}, HTTP_HOST=self.host)
        self.assertEqual(self._prices(), (Decimal('300.00'), Decimal('500.00'), Decimal('450.00')))

    def test_missing_fields_keep_their_value(self):
        c = self._client('manager')
        c.post(f'/system/products/{self.product.pk}/prices/', {'retail_price': '550'}, HTTP_HOST=self.host)
        self.assertEqual(self._prices(), (Decimal('300.00'), Decimal('550.00'), Decimal('450.00')))

    def test_cost_hidden_and_locked_for_staff_without_cost_access(self):
        # المشتريات بتعدّل سعر البيع، بس التكلفة مقفولة عليها من غير can_see_costs
        c = self._client('purchasing')
        page = c.get(f'/system/products/{self.product.pk}/gallery/', HTTP_HOST=self.host)
        self.assertNotContains(page, 'name="purchase_price"')
        page = c.get(f'/system/products/{self.product.pk}/edit/', HTTP_HOST=self.host)
        self.assertNotContains(page, 'name="purchase_price"')
        c.post(f'/system/products/{self.product.pk}/prices/',
               {'purchase_price': '1', 'retail_price': '520'}, HTTP_HOST=self.host)
        self.assertEqual(self._prices()[:2], (Decimal('300.00'), Decimal('520.00')))
