"""✉️ تأكيد الإيميل في التسجيل بقى حقيقي (clients/services/email_verification.py).

قبل كده صفحة النجاح كانت بتعرض رابط التفعيل وبتحوّل عليه لوحدها — أي حد يسجّل
بإيميل مش بتاعه والشركة تتفعّل ويدخل كمالك. دلوقتي لو الإرسال متظبط الرابط
بيروح على الإيميل بس؛ ولو مش متظبط أو فشل، بيظهر على الشاشة (محدش يتقفل برّه).
"""
import os
import re
from unittest import mock

from django.core import mail
from django.db import connection
from django.test import Client as HttpClient, SimpleTestCase, override_settings

from clients.models import Client, Domain
from clients.services import email_verification as ev
from inventory.tests.base import ERPTenantTestCase

FORM = {
    'company_name': 'ورشة التأكيد', 'industry': 'automotive', 'business_type': 'service_center',
    'full_name': 'سعيد محمد', 'email': 'owner@verify.test', 'phone': '01000000000',
    'password': 'Str0ng#Pass2026', 'website_url_honeypot': '', 'subdomain': 'verify-shop',
}
LINK = re.compile(r'/account/verify-email/\?token=([\w:.\-]+)')


@override_settings(ALLOWED_HOSTS=['*'], EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class SignupEmailVerificationTests(ERPTenantTestCase):

    def setUp(self):
        self.env = mock.patch.dict(os.environ, {'BASE_DOMAIN': 'test.com'})
        self.env.start()
        from django.core.cache import cache
        cache.delete('signup_ip:127.0.0.1')   # حد التسجيلات لنفس الجهاز
        cache.delete(f"verify_resend:{FORM['email']}")
        self.c = HttpClient()

    def tearDown(self):
        self.env.stop()
        connection.set_schema_to_public()
        from clients.models import TenantSubscription
        for t in Client.all_objects.filter(schema_name__startswith='verify_shop'):
            TenantSubscription.objects.filter(tenant=t).delete()
            Domain.objects.filter(tenant=t).delete()
            t.delete(force_drop=True)
        connection.set_tenant(self.tenant)
        super().tearDown()

    def signup(self):
        r = self.c.post('/connect/signup/', FORM, HTTP_HOST=self.domain.domain)
        self.assertEqual(r.status_code, 200)
        connection.set_schema_to_public()
        tenant = Client.all_objects.get(schema_name='verify_shop')
        return r.content.decode(), tenant

    @override_settings(SIGNUP_EMAIL_VERIFICATION='required')
    def test_link_goes_only_to_the_inbox_when_email_works(self):
        html, tenant = self.signup()
        self.assertFalse(tenant.is_active)
        self.assertIsNone(LINK.search(html), 'رابط التفعيل ظاهر في الصفحة')
        self.assertIn('owner@verify.test', html)
        self.assertNotIn('id="autoRedirectBtn"', html)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ['owner@verify.test'])
        token = LINK.search(mail.outbox[0].body).group(1)

        # الرابط اللي في الإيميل هو اللي بيفعّل (على أي دومين — test.com مش متسجّل هنا)
        connection.set_tenant(self.tenant)
        r = self.c.get('/account/verify-email/', {'token': token}, HTTP_HOST=self.domain.domain)
        connection.set_schema_to_public()
        self.assertEqual(r.status_code, 200)
        tenant.refresh_from_db()
        self.assertTrue(tenant.is_active)

    @override_settings(SIGNUP_EMAIL_VERIFICATION='required')
    def test_resend_from_the_success_page_sends_a_new_link(self):
        self.signup()
        connection.set_tenant(self.tenant)
        r = self.c.post('/account/verify-email/resend/', {'email': FORM['email']}, HTTP_HOST=self.domain.domain)
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(LINK.search(r.content.decode()))
        self.assertEqual(len(mail.outbox), 2)
        self.assertRegex(mail.outbox[1].body, LINK)

    @override_settings(SIGNUP_EMAIL_VERIFICATION='required')
    def test_failed_send_falls_back_to_the_on_screen_link(self):
        with mock.patch('django.core.mail.message.EmailMessage.send', side_effect=OSError('smtp blocked')):
            html, tenant = self.signup()
        self.assertFalse(tenant.is_active)
        self.assertIsNotNone(LINK.search(html), 'العميل اتقفل برّه: لا إيميل ولا رابط')

    def test_auto_mode_without_email_settings_keeps_the_on_screen_link(self):
        self.assertFalse(ev.verification_required())   # locmem = مفيش إرسال حقيقي
        html, _ = self.signup()
        self.assertIsNotNone(LINK.search(html))


class VerificationModeTests(SimpleTestCase):

    @override_settings(EMAIL_BACKEND='erp_core.email_backends.BrevoAPIEmailBackend', BREVO_API_KEY='xkeysib-abc')
    def test_brevo_api_key_means_delivery_is_configured(self):
        self.assertTrue(ev.delivery_configured())
        self.assertTrue(ev.verification_required())

    @override_settings(EMAIL_BACKEND='erp_core.email_backends.BrevoAPIEmailBackend', BREVO_API_KEY='xsmtpsib-abc')
    def test_brevo_smtp_key_is_not_an_api_key(self):
        self.assertFalse(ev.delivery_configured())

    @override_settings(EMAIL_BACKEND='django.core.mail.backends.smtp.EmailBackend',
                       EMAIL_HOST='smtp.x.com', EMAIL_HOST_USER='u', EMAIL_HOST_PASSWORD='')
    def test_smtp_without_password_is_not_configured(self):
        self.assertFalse(ev.delivery_configured())

    @override_settings(EMAIL_BACKEND='erp_core.email_backends.BrevoAPIEmailBackend', BREVO_API_KEY='xkeysib-abc',
                       SIGNUP_EMAIL_VERIFICATION='off')
    def test_off_wins(self):
        self.assertFalse(ev.verification_required())
