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


def _phone() -> str:
    return f'+2010{uuid.uuid4().int % 100_000_000:08d}'


class SoftDeletedLoginTests(TestCase):
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
