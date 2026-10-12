"""
🏢 New-company signup works from any host and honours the chosen link.

The signup page is served on every subdomain. Opened from inside an existing
shop (``fixit-02e0.mousstec.com/connect/signup/``) the connection is on that
shop's schema and django-tenants refuses to create a tenant there («Can't
create tenant outside the public schema») → «تعذر بناء مساحة العمل». The
verification link was built on the same shop host, where activating the new
tenant failed the same way. The «رابط النظام» the customer typed was ignored.
"""
import os
import re
from unittest import mock

from django.db import connection
from django.test import Client as HttpClient, override_settings

from clients.models import Client, Domain
from .base import ERPTenantTestCase

FORM = {
    'company_name': 'جملة للسيارات', 'industry': 'automotive', 'business_type': 'service_center',
    'full_name': 'سعيد محمد', 'email': 'gom@example.com', 'phone': '01000000000',
    'password': 'Str0ng#Pass2026', 'website_url_honeypot': '',
}


@override_settings(ALLOWED_HOSTS=['*'], EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class SignupFromShopSubdomainTests(ERPTenantTestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {'BASE_DOMAIN': 'test.com'})
        self.env.start()
        from django.core.cache import cache
        cache.delete('signup_ip:127.0.0.1')   # حد ٥ تسجيلات/ساعة لنفس الجهاز
        self.c = HttpClient()

    def tearDown(self):
        self.env.stop()
        connection.set_schema_to_public()
        from clients.models import TenantSubscription
        for t in Client.all_objects.filter(schema_name__startswith='gom_auto'):
            TenantSubscription.objects.filter(tenant=t).delete()
            Domain.objects.filter(tenant=t).delete()
            t.delete(force_drop=True)
        connection.set_tenant(self.tenant)
        super().tearDown()

    def test_signup_inside_a_shop_builds_the_requested_workspace(self):
        r = self.c.post('/connect/signup/', {**FORM, 'subdomain': 'Gom Auto'}, HTTP_HOST=self.domain.domain)
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        self.assertNotIn('تعذر بناء مساحة العمل', html)
        connection.set_schema_to_public()
        new = Client.all_objects.get(schema_name='gom_auto')
        self.assertFalse(new.is_active)
        self.assertTrue(Domain.objects.filter(tenant=new, domain='gom-auto.test.com').exists())

        link = re.search(r'https?://([^/"\s]+)/account/verify-email/\?token=([^"&\s<]+)', html)
        self.assertEqual(link.group(1), 'test.com')     # مش على دومين الشركة اللي اتسجّل منها
        # ولو اتفتح على subdomain شركة برضه بيفعّل
        connection.set_tenant(self.tenant)
        r = self.c.get('/account/verify-email/', {'token': link.group(2)}, HTTP_HOST=self.domain.domain)
        self.assertEqual(r.status_code, 200)
        connection.set_schema_to_public()
        new.refresh_from_db()
        self.assertTrue(new.is_active)

    def test_taken_reserved_or_invalid_link_is_explained(self):
        taken = self.domain.domain.split('.')[0]
        for sub, msg in ((taken, 'محجوز'), ('www', 'محجوز'), ('1ab', 'ويبدأ بحرف'), ('ب', 'ويبدأ بحرف')):
            r = self.c.post('/connect/signup/', {**FORM, 'subdomain': sub}, HTTP_HOST=self.domain.domain)
            self.assertIn(msg, r.content.decode(), sub)
        connection.set_schema_to_public()
        self.assertFalse(Client.all_objects.filter(email='gom@example.com').exists())
        connection.set_tenant(self.tenant)
