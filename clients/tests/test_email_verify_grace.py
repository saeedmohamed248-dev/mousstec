"""✉️ تأكيد الإيميل للشركة الجديدة.

العميل الجديد بيدخل سيستمه على طول بعد التأسيس، ويظهرله شريط «أكّد إيميلك».
🐛 كان بعد ٤٨ ساعة من غير تأكيد الدخول بيتقفل خالص — ٣ من ٢٠ شركة تجريبية اتقفلوا كده
ومارجعوش (منهم شركة كانت شغّالة). دلوقتي مفيش قفل: الشريط بيفضل ظاهر لحد التأكيد.
"""
import os
import re
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client as HttpClient, override_settings
from django.utils import timezone
from django_tenants.utils import get_tenant_domain_model, schema_context

from clients.models import Client
from clients.services import email_verification as ev
from inventory.tests.base import ERPTenantTestCase

FORM = {'company_name': 'ورشة المهلة', 'full_name': 'سعيد محمد', 'email': 'owner@grace.test',
        'phone': '01094850764', 'password': 'Str0ng#Pass2026', 'website_url_honeypot': '',
        'subdomain': 'grace-shop', 'industry': 'automotive', 'business_type': 'service_center'}
LINK = re.compile(r'/account/verify-email/\?token=([\w:.\-]+)')
EMAIL_ON = dict(EMAIL_BACKEND='erp_core.email_backends.BrevoAPIEmailBackend', BREVO_API_KEY='xkeysib-abc')


@override_settings(ALLOWED_HOSTS=['*'])
class EmailVerifyGraceTests(ERPTenantTestCase):

    def setUp(self):
        super().setUp()
        from django.core.cache import cache
        self.env = mock.patch.dict(os.environ, {'BASE_DOMAIN': 'test.com'})
        self.env.start()
        connection.set_schema_to_public()
        cache.delete('signup_ip:127.0.0.1')
        cache.delete(f"verify_resend:{FORM['email']}")

    def tearDown(self):
        self.env.stop()
        connection.set_schema_to_public()
        from clients.models import Domain, TenantSubscription
        for t in Client.all_objects.filter(email=FORM['email']):
            TenantSubscription.objects.filter(tenant=t).delete()
            Domain.objects.filter(tenant=t).delete()
            t.delete(force_drop=True)
        connection.set_tenant(self.tenant)
        super().tearDown()

    def _signup(self):
        r = HttpClient().post('/connect/signup/', FORM, HTTP_HOST=self.domain.domain)
        self.assertEqual(r.status_code, 200, r.get('Location'))
        connection.set_schema_to_public()
        shop = Client.all_objects.get(email=FORM['email'])
        host = get_tenant_domain_model().objects.filter(tenant=shop).first().domain
        with schema_context(shop.schema_name):
            owner = get_user_model().objects.get(email=FORM['email'])
        connection.set_schema_to_public()
        # الدخول بنفس الرابط اللي صفحة «تم التأسيس» بتحوّل عليه (المسار الحقيقي)
        html = r.content.decode()
        link = re.search(r'href="https?://[^/"]+(/auto-login/\?token=[^"]+)"', html)
        self.assertIsNotNone(link, 'صفحة التأسيس مافيهاش دخول مباشر')
        c = HttpClient(HTTP_HOST=host)
        r2 = c.get(link.group(1).replace('&amp;', '&'))
        self.assertEqual(r2.status_code, 302)
        self.assertEqual(r2['Location'], '/system/dashboard/')
        connection.set_schema_to_public()
        self.owner_id = owner.pk
        return shop, c, html

    def _expire(self, shop):
        shop.email_verify_deadline = timezone.now() - timedelta(minutes=1)
        shop.save(update_fields=['email_verify_deadline'])

    @override_settings(**EMAIL_ON)
    def test_enters_right_away_with_a_48h_banner(self):
        with mock.patch.object(ev, 'send_verification_email', return_value=True):
            shop, c, html = self._signup()
        self.assertTrue(shop.is_active)
        self.assertIn('/auto-login/?token=', html)
        left = shop.email_verify_deadline - timezone.now()
        self.assertTrue(timedelta(hours=47) < left <= timedelta(hours=48))

        r = c.get('/system/dashboard/')
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'id="emailVerifyBanner"')
        self.assertContains(r, '/account/verify-email/resend/')

    @override_settings(**EMAIL_ON)
    def test_after_48h_access_stays_open_with_banner_until_the_link_is_clicked(self):
        with mock.patch.object(ev, 'send_verification_email', return_value=True):
            shop, c, _ = self._signup()
        self._expire(shop)

        # المهلة خلصت ⇒ الدخول شغّال عادي (سيارات + لوحة الأدمن) والشريط لسه ظاهر
        r = c.get('/system/dashboard/')
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'id="emailVerifyBanner"')
        self.assertNotEqual(c.get('/secure-portal/').status_code, 403)

        # «ابعت الرابط تاني» شغّال
        with mock.patch.object(ev, 'send_verification_email', return_value=True) as send, \
                mock.patch('clients.views.auth_views._throttle', return_value=(False, 0)):
            r = c.post('/account/verify-email/resend/', {'email': FORM['email']})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(send.called)
        token = LINK.search(send.call_args.kwargs['url']).group(1)

        # الضغط على الرابط بيشيل الشريط
        r = c.get('/account/verify-email/', {'token': token})
        self.assertEqual(r.status_code, 200)
        connection.set_schema_to_public()
        shop.refresh_from_db()
        self.assertIsNone(shop.email_verify_deadline)
        r = c.get('/system/dashboard/')
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, 'id="emailVerifyBanner"')

    @override_settings(**EMAIL_ON)
    def test_signup_login_link_survives_more_than_5_minutes(self):
        """🐛 توكن «تم التأسيس» كان بيخلص بعد ٥ دقايق ويودّي على لوحة Django الفنية."""
        import time as _time
        with mock.patch.object(ev, 'send_verification_email', return_value=True):
            r = HttpClient().post('/connect/signup/', FORM, HTTP_HOST=self.domain.domain)
        connection.set_schema_to_public()
        shop = Client.all_objects.get(email=FORM['email'])
        host = get_tenant_domain_model().objects.filter(tenant=shop).first().domain
        connection.set_schema_to_public()
        link = re.search(r'href="https?://[^/"]+(/auto-login/\?token=[^"]+)"', r.content.decode()).group(1)
        link = link.replace('&amp;', '&')
        real = _time.time
        with mock.patch('time.time', return_value=real() + 20 * 60):      # بعد ٢٠ دقيقة لسه شغّال
            r2 = HttpClient(HTTP_HOST=host).get(link)
        self.assertEqual((r2.status_code, r2['Location']), (302, '/system/dashboard/'))
        connection.set_schema_to_public()
        with mock.patch('time.time', return_value=real() + 40 * 60):      # بعد ٤٠ دقيقة ⇒ صفحة الدخول العادية
            r3 = HttpClient(HTTP_HOST=host).get(link)
        self.assertEqual(r3['Location'], '/login/?msg=token_expired')
        connection.set_schema_to_public()

    def test_no_deadline_when_email_cannot_be_delivered(self):
        """من غير إرسال إيميل حقيقي محدش يتقفل برّه حسابه بسبب إيميل مابيوصلش."""
        self.assertFalse(ev.delivery_configured())
        shop, c, _ = self._signup()
        self.assertTrue(shop.is_active)
        self.assertIsNone(shop.email_verify_deadline)
        self.assertEqual(c.get('/system/dashboard/').status_code, 200)

    def test_existing_companies_are_never_locked(self):
        """الشركات اللي اتسجّلت قبل الميزة (المهلة فاضية) مابتتأثرش."""
        self.assertIsNone(self.tenant.email_verify_deadline)
        self.assertFalse(self.tenant.email_verification_expired)
