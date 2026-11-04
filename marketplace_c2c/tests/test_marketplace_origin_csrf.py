"""🛡️ CSRF لعملاء السوق: طلب بيغيّر بيانات بكوكي mp_session لازم يكون من نفس الـ host."""
from __future__ import annotations

import json
import uuid

from django.http import HttpResponse
from django.test import Client, RequestFactory, SimpleTestCase, TestCase, override_settings

from clients.models import MarketplaceCustomer
from erp_core.marketplace_csrf import MarketplaceOriginCheckMiddleware
from marketplace_c2c.tests.test_parts_order_lifecycle import _PublicDomainMixin

HOST = 'mousstec.com'


def _mw():
    return MarketplaceOriginCheckMiddleware(lambda r: HttpResponse('ok'))


@override_settings(ALLOWED_HOSTS=['*'], MARKETPLACE_TRUSTED_ORIGINS=[])
class OriginCheckUnitTests(SimpleTestCase):
    def _req(self, method='post', cookie=True, **meta):
        rf = RequestFactory()
        req = getattr(rf, method)('/marketplace/x/', HTTP_HOST=HOST, **meta)
        if cookie:
            req.COOKIES['mp_session'] = str(uuid.uuid4())
        return req

    def _status(self, req):
        return _mw()(req).status_code

    def test_same_origin_allowed(self):
        self.assertEqual(self._status(self._req(HTTP_ORIGIN=f'https://{HOST}')), 200)
        self.assertEqual(self._status(self._req(HTTP_ORIGIN=f'https://{HOST}:443')), 200)

    def test_sibling_subdomain_blocked(self):
        # same-site ⇒ SameSite=Lax بيبعت الكوكي — ده بالظبط الهجوم اللي كان مفتوح
        self.assertEqual(self._status(self._req(HTTP_ORIGIN=f'https://evil-shop.{HOST}')), 403)

    def test_foreign_and_null_origin_blocked(self):
        self.assertEqual(self._status(self._req(HTTP_ORIGIN='https://attacker.example')), 403)
        self.assertEqual(self._status(self._req(HTTP_ORIGIN='null')), 403)

    def test_referer_used_when_no_origin(self):
        self.assertEqual(self._status(self._req(HTTP_REFERER=f'https://{HOST}/marketplace/')), 200)
        self.assertEqual(self._status(self._req(HTTP_REFERER=f'https://x.{HOST}/')), 403)

    def test_no_headers_allowed_non_browser_client(self):
        self.assertEqual(self._status(self._req()), 200)

    def test_without_cookie_or_safe_method_untouched(self):
        self.assertEqual(self._status(self._req(cookie=False, HTTP_ORIGIN='https://attacker.example')), 200)
        self.assertEqual(self._status(self._req(method='get', HTTP_ORIGIN='https://attacker.example')), 200)

    @override_settings(MARKETPLACE_TRUSTED_ORIGINS=['https://app.partner.test'])
    def test_trusted_origin_allowed(self):
        self.assertEqual(self._status(self._req(HTTP_ORIGIN='https://app.partner.test')), 200)


class OriginCheckEndToEndTests(_PublicDomainMixin, TestCase):
    """على endpoint حقيقي: مسح لوجو البراند (كان csrf_exempt ومحمي بالكوكي بس)."""

    def setUp(self):
        self.customer = MarketplaceCustomer.objects.create(
            customer_type='individual', full_name='CSRF', phone=f'+2010{uuid.uuid4().int % 100_000_000:08d}',
            sector='printing', is_verified=True, email=f'{uuid.uuid4().hex[:8]}@csrf.test')
        self.c = Client()
        self.c.cookies['mp_session'] = str(self.customer.session_token)

    def test_cross_subdomain_post_blocked_same_host_passes(self):
        r = self.c.post('/marketplace/brand-profile/logo/primary/delete/', HTTP_ORIGIN='https://evil.testserver')
        self.assertEqual(r.status_code, 403)
        self.assertEqual(json.loads(r.content)['code'], 'csrf_origin')
        r = self.c.post('/marketplace/brand-profile/logo/primary/delete/', HTTP_ORIGIN='http://testserver')
        self.assertNotEqual(r.status_code, 403)
