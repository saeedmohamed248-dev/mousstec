"""
🏷️ صفحة الأصناف: «جملة» تحت سعر البيع كانت بتتقري على إنها سعر الشراء.

طابلوه اتشترى بـ 35,000 + مصاريف شحنة وسعر بيعه جملة 30,000: خانة التكلفة كانت
بتعرض «35,000 + 16,672 مصاريف» من غير ما تقول إن الـ 35,000 ده سعر الشراء، وتحت
سعر البيع «جملة: 30,000» — شكلهم رقمين مختلفين لنفس الحاجة، والبيع جملة بالسعر ده
خسارة ومفيش أي تنبيه.
"""
import json
from decimal import Decimal

from django.contrib.auth.models import User
from django.db import connection
from django.test import Client, override_settings

from .base import ERPTenantTestCase
from .factories import make_branch, make_product, make_treasury, make_vendor


@override_settings(ALLOWED_HOSTS=['*'])
class WholesaleLabelTests(ERPTenantTestCase):
    def test_purchase_and_wholesale_sale_price_are_labelled_and_loss_is_flagged(self):
        connection.set_tenant(self.tenant)
        host = self.domain.domain
        branch = make_branch()
        make_treasury(branch)
        part = make_product(part_number='WL-1', name='طابلوه g05', retail_price='60000',
                            purchase_price='0', average_cost='0', b2b_wholesale_price=Decimal('30000'))
        ok = make_product(part_number='WL-2', name='فلتر', retail_price='300',
                          purchase_price='0', average_cost='0', b2b_wholesale_price=Decimal('250'))
        c = Client()
        c.force_login(User.objects.create_user('wl_boss', password='x', is_staff=True, is_superuser=True))
        body = {'vendor_id': make_vendor().pk, 'branch_id': branch.pk, 'treasury_id': None, 'paid_amount': 0,
                'items': [{'product_id': part.pk, 'qty': 1, 'cost': 35000},
                          {'product_id': ok.pk, 'qty': 1, 'cost': 100}],
                'extra_costs': [{'kind': 'shipping', 'behavior': 'landed', 'amount': 1000}]}
        r = c.post('/system/purchases/save/', json.dumps(body), content_type='application/json', HTTP_HOST=host)
        self.assertEqual(r.status_code, 200, r.content)

        html = c.get('/system/products/', HTTP_HOST=host).content.decode()
        self.assertIn('شراء 35,000.00', html)                       # الـ 35,000 متسمّي «شراء»
        self.assertIn('بيع جملة: 30,000.00', html)
        self.assertIn('(أقل من التكلفة)', html)                     # 30,000 < 35,000 + مصاريف
        self.assertEqual(html.count('(أقل من التكلفة)'), 1)          # الفلتر (250 > تكلفته) مش متعلّم
        self.assertIn('1 صنف سعر بيع الجملة بتاعه أقل من تكلفته', html)
