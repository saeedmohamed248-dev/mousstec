"""
🛡️ Platform-owner endpoints must not trust a shop owner.

Every shop owner is created as ``is_superuser`` inside their own schema, and
the platform URLs are served on every subdomain. Endpoints that only checked
``user.is_superuser`` therefore let any shop owner act as the platform owner:
approve their own manual subscription receipt (free subscription), gift
themselves diagnostics/OBD, open platform-wide tables in the Django admin
(other shops' tickets, staff roles…).
"""
from decimal import Decimal

from django.contrib.auth.models import User
from django.db import connection
from django.test import Client, override_settings

from clients.models import ManualPaymentReceipt
from .base import ERPTenantTestCase


@override_settings(ALLOWED_HOSTS=['*'])
class PlatformOwnerGateTests(ERPTenantTestCase):
    def setUp(self):
        connection.set_tenant(self.tenant)
        self.host = self.domain.domain
        self.shop_owner = User.objects.create_user('shop_owner', password='x', is_staff=True, is_superuser=True)
        self.c = Client()
        self.c.force_login(self.shop_owner)

    def test_shop_owner_cannot_confirm_own_receipt(self):
        connection.set_schema_to_public()
        receipt = ManualPaymentReceipt.objects.create(
            purchase_type='subscription', purchase_id=1, amount=Decimal('500'),
            sender_phone='01000000000', txn_reference='FAKE', tenant=self.tenant)
        connection.set_tenant(self.tenant)
        r = self.c.post(f'/payment/manual/admin/review/{receipt.receipt_code}/',
                        {'action': 'confirm'}, HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 403)
        connection.set_schema_to_public()
        receipt.refresh_from_db()
        self.assertEqual(receipt.status, 'pending')
        connection.set_tenant(self.tenant)

    def test_shop_owner_is_locked_out_of_platform_pages(self):
        for method, url in [
            ('get', '/superadmin/gift-diagnostics/'),
            ('post', f'/superadmin/tenant/{self.tenant.pk}/obd-grant/'),
            ('post', f'/superadmin/tenant/{self.tenant.pk}/diag-topup-grant/'),
            ('get', '/superadmin/wix/'),
            ('get', '/admin-tools/system-health/'),
            ('post', '/marketplace/admin/approve/1/'),
        ]:
            r = getattr(self.c, method)(url, HTTP_HOST=self.host)
            self.assertIn(r.status_code, (302, 403), f'{url} → {r.status_code}')
            if r.status_code == 302:
                self.assertIn('login', r['Location'], url)

    def test_platform_tables_closed_in_django_admin_on_a_subdomain(self):
        for model in ('staffrole', 'supportticket', 'chatsession', 'systemerrorlog', 'wixconnection'):
            r = self.c.get(f'/secure-portal/clients/{model}/', HTTP_HOST=self.host)
            self.assertIn(r.status_code, (302, 403, 404), f'{model} → {r.status_code}')
            r = self.c.get(f'/secure-portal/clients/{model}/add/', HTTP_HOST=self.host)
            self.assertIn(r.status_code, (302, 403, 404), f'{model}/add → {r.status_code}')
