"""💸 باقات الأرقام الإضافية: الشراء لازم يزوّد السعة — مش يخصم من المحفظة على الفاضي.

🐛 قبل كده: نفس الباقة تاني كانت بتتخصم والسعة زي ما هي، والباقة الأصغر بعد
الأكبر كانت بتتخصم وتقلّل السعة.
"""
import uuid
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.contrib.messages.storage.fallback import FallbackStorage
from django.test import RequestFactory, TestCase

from clients.models import Client, EscrowLedger
from omnichannel import console_views
from omnichannel.models import TenantChannelConfig

_buy = console_views.console_buy_numbers.__wrapped__.__wrapped__   # من غير حارس الاشتراك/الدخول


class NumberPackagePurchaseTests(TestCase):

    def setUp(self):
        patcher = mock.patch.object(Client, 'auto_create_schema', False)
        patcher.start()
        self.addCleanup(patcher.stop)
        schema = f'tst_{uuid.uuid4().hex[:8]}'
        Client.objects.bulk_create([Client(schema_name=schema, name='شركة', owner_name='م',
                                           phone='01000000000', wallet_balance=Decimal('5000'))])
        self.tenant = Client.objects.get(schema_name=schema)
        self.config = TenantChannelConfig.objects.create(tenant=self.tenant)
        self.owner = User.objects.create_user(f'own_{schema}', password='x', is_superuser=True, is_staff=True)

    def buy(self, extra):
        request = RequestFactory().post('/x/', {'extra': str(extra)})
        request.user = self.owner
        request.session = {}
        request._messages = FallbackStorage(request)
        request.omni_tenant = self.tenant
        request.omni_config = self.config
        return _buy(request)

    def test_buying_the_same_or_a_smaller_package_charges_nothing(self):
        self.buy(4)
        self.tenant.refresh_from_db()
        self.config.refresh_from_db()
        self.assertEqual(self.config.extra_numbers, 4)
        paid = Decimal('5000') - self.tenant.wallet_balance
        self.assertGreater(paid, 0)
        self.buy(4)
        self.buy(2)
        self.tenant.refresh_from_db()
        self.config.refresh_from_db()
        self.assertEqual(self.config.extra_numbers, 4)                       # السعة ماقلّتش
        self.assertEqual(Decimal('5000') - self.tenant.wallet_balance, paid)  # ولا اتخصم تاني
        self.assertEqual(EscrowLedger.objects.filter(client=self.tenant, transaction_type='fee_deduction').count(), 1)

    def test_upgrading_still_works(self):
        self.buy(2)
        self.buy(4)
        self.config.refresh_from_db()
        self.assertEqual(self.config.extra_numbers, 4)
        self.assertEqual(EscrowLedger.objects.filter(client=self.tenant, transaction_type='fee_deduction').count(), 2)
