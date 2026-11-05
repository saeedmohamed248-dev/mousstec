"""🐛 أعطال الكاشير وأمر الشغل كانت بتظهر في «سجل أخطاء النظام» كـ 500 فاضي.

الشاشات دي بتمسك الاستثناء جوّاها وترجّع JSON بـ 500 (والعميل بيشوف «حدث خطأ
داخلي») — فالمسجّل ماكانش بيشوف الاستثناء ولا الرسالة، ومستحيل تعرف إيه اللي وقع.
دلوقتي `_json_response_safe` بيلزق الرسالة الأصلية والـ traceback على الرد والمسجّل بيحفظهم.
"""
import json
from unittest import mock

from django.contrib.auth.models import User
from django.db import connection
from django.test import Client, override_settings
from django_tenants.utils import schema_context

from clients.models import SystemErrorLog
from .base import ERPTenantTestCase
from .factories import make_branch, make_inventory, make_product, make_treasury


@override_settings(ALLOWED_HOSTS=['*'], DEBUG=False)
class CaughtServerErrorLoggingTests(ERPTenantTestCase):
    def setUp(self):
        connection.set_tenant(self.tenant)
        self.host = self.domain.domain
        self.branch = make_branch()
        self.till = make_treasury(self.branch, balance='5000.00')
        self.part = make_product(part_number='LOG-500', retail_price='100.00', average_cost='60.00')
        make_inventory(self.part, self.branch, quantity=5)
        self.boss = User.objects.create_user('log500_boss', password='x', is_staff=True, is_superuser=True)
        self.c = Client()
        self.c.force_login(self.boss)
        with schema_context('public'):
            SystemErrorLog.objects.filter(path='/system/lightning-pos/checkout/').delete()

    def test_pos_checkout_crash_is_logged_with_message_and_traceback(self):
        with mock.patch('inventory.views_lightning.pos._post_sale_to_ledger',
                        side_effect=RuntimeError('ledger exploded')):
            r = self.c.post('/system/lightning-pos/checkout/', json.dumps({
                'branch_id': self.branch.pk,
                'items': [{'product_id': self.part.pk, 'qty': 1, 'price': 100}],
                'treasury_id': self.till.pk, 'paid_amount': 100}),
                content_type='application/json', HTTP_ACCEPT='application/json', HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 500)
        # العميل مابيشوفش تفاصيل داخلية في الإنتاج
        self.assertNotIn('ledger exploded', r.content.decode())

        with schema_context('public'):
            log = SystemErrorLog.objects.filter(path='/system/lightning-pos/checkout/').latest('pk')
        self.assertIn('ledger exploded', log.message)
        self.assertEqual(log.exception_class, 'RuntimeError')
        self.assertIn('Traceback', log.traceback)
        self.assertIn('_post_sale_to_ledger', log.traceback)
