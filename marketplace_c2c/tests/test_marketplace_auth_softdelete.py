"""
Security regression test: soft-deleted MarketplaceCustomer accounts
must not be able to authenticate via either:
  (a) the session-cookie path used by every marketplace view, or
  (b) the login endpoint itself.
"""
from __future__ import annotations

import json
import uuid

from django.test import Client, RequestFactory, TestCase

from clients.models import MarketplaceCustomer
from clients.views._shared import _marketplace_auth
from marketplace_c2c.tests.test_parts_order_lifecycle import _PublicDomainMixin


def _phone() -> str:
    return f'+2010{uuid.uuid4().int % 100_000_000:08d}'


# 🐛 [FIX]: من غير _PublicDomainMixin الطلب كان بيرجع 404 من الـ routing نفسه (مش من الـ view)
# فالاختبار كان بينجح من غير ما يختبر حاجة.
class SoftDeletedLoginTests(_PublicDomainMixin, TestCase):
    def setUp(self):
        self.password = 'CorrectHorseBattery!42'
        self.phone = _phone()
        self.customer = MarketplaceCustomer.objects.create(
            customer_type='individual',
            full_name='Deep',
            phone=self.phone,
            sector='automotive',
            is_verified=True,
        )
        self.customer.set_password(self.password)
        self.customer.save()

    # (a) Session-cookie path
    def test_marketplace_auth_returns_none_for_deleted_customer(self):
        token = str(self.customer.session_token)
        rf = RequestFactory()
        request = rf.get('/marketplace/parts/')
        request.COOKIES['mp_session'] = token
        # Sanity: live customer authenticates fine.
        self.assertEqual(_marketplace_auth(request).pk, self.customer.pk)

        # Soft delete and re-check.
        self.customer.soft_delete(reason='security test')
        self.assertIsNone(_marketplace_auth(request))

    # (b) Direct login endpoint
    def test_login_endpoint_rejects_deleted_account(self):
        self.customer.soft_delete()
        c = Client()
        resp = c.post(
            '/marketplace/login/',
            data=json.dumps({'phone': self.phone, 'password': self.password}),
            content_type='application/json',
        )
        # Must NOT return 200 — anything in 4xx is acceptable security-wise.
        self.assertNotEqual(resp.status_code, 200)
        self.assertIn(resp.status_code, (403, 404))
        # And must not leak a session cookie.
        self.assertNotIn('mp_session', resp.cookies)


class PerPhoneLoginLockoutTests(_PublicDomainMixin, TestCase):
    """🛡️ تخمين باسورد رقم معيّن من IPs مختلفة لازم يتقفل بعد ١٠ محاولات غلط."""

    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        self.phone = _phone()
        self.customer = MarketplaceCustomer.objects.create(
            customer_type='individual', full_name='Lock', phone=self.phone,
            sector='automotive', is_verified=True,
        )
        self.customer.set_password('Right-pass-123')
        self.customer.save()

    def _login(self, password, ip):
        return Client().post(
            '/marketplace/login/',
            data=json.dumps({'phone': self.phone, 'password': password}),
            content_type='application/json', REMOTE_ADDR=ip,
        )

    def test_wrong_passwords_from_many_ips_lock_the_phone(self):
        for i in range(10):
            self.assertEqual(self._login('wrong', f'10.0.0.{i + 1}').status_code, 403)
        # حتى الباسورد الصح من IP جديد بيترفض لحد ما المدة تخلص
        self.assertEqual(self._login('Right-pass-123', '10.0.1.1').status_code, 429)

    def test_successful_login_resets_the_counter(self):
        for i in range(5):
            self._login('wrong', f'10.0.0.{i + 1}')
        self.assertEqual(self._login('Right-pass-123', '10.0.2.1').status_code, 200)
        from django.core.cache import cache
        self.assertIsNone(cache.get(f'mp_login_fail_phone:{self.phone}'))
