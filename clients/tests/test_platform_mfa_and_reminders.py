"""🔐 التحقق بخطوتين لحساب مالك المنصة + 📱 تذكير التجديد بالواتساب.

قبل التصليح:
- جدول التحقق بخطوتين كان في سكيمات الشركات بس — مالك المنصة (أهم حساب) ماكانش يقدر يفعّله،
  وصفحة /account/mfa/ على الدومين الرئيسي كانت بتقع 500.
- تذكير التجديد بالواتساب كان بيتكتب في اللوج بس — من غير SMTP مفيش أي تذكير قبل الإيقاف.
"""
from io import StringIO
from unittest import mock

import pyotp
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import Client as HttpClient, TestCase, override_settings

from clients.models import PlatformEvent, PlatformUserMFA
from clients.services.whatsapp_notify import normalize_phone
from marketplace_c2c.tests.test_parts_order_lifecycle import _PublicDomainMixin

User = get_user_model()


@override_settings(ALLOWED_HOSTS=['*'])
class PlatformOwnerMFATests(_PublicDomainMixin, TestCase):

    def setUp(self):
        self.owner = User.objects.create_user('owner@platform.test', password='x', is_superuser=True,
                                              is_staff=True)

    def _client(self):
        c = HttpClient()
        c.force_login(self.owner)
        return c

    def _enable(self):
        c = self._client()
        r = c.get('/account/mfa/')
        self.assertEqual(r.status_code, 200)                       # قبل كده 500 على الدومين الرئيسي
        mfa = PlatformUserMFA.objects.get(user=self.owner)
        r = c.post('/account/mfa/', {'action': 'verify_enable', 'code': pyotp.TOTP(mfa.secret).now()})
        self.assertEqual(r.status_code, 200)
        mfa.refresh_from_db()
        self.assertTrue(mfa.is_enabled)
        return mfa, r.context['backup_codes']

    def test_owner_can_enable_and_every_new_session_needs_the_code(self):
        mfa, _codes = self._enable()
        c = self._client()                                          # جلسة جديدة (دخول بكلمة السر)
        r = c.get('/superadmin/')
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r['Location'].startswith('/account/mfa/verify/?next=/superadmin/'))
        r = c.post('/account/mfa/verify/', {'code': '000000', 'next': '/superadmin/'})
        self.assertContains(r, 'الكود غير صحيح')
        r = c.post('/account/mfa/verify/', {'code': pyotp.TOTP(mfa.secret).now(), 'next': '/superadmin/'})
        self.assertEqual(r['Location'], '/superadmin/')
        self.assertFalse(c.get('/superadmin/').get('Location', '').startswith('/account/mfa/verify/'))

    def test_backup_code_works_once(self):
        _mfa, codes = self._enable()
        c = self._client()
        r = c.post('/account/mfa/verify/', {'code': codes[0], 'next': '/superadmin/'})
        self.assertEqual(r['Location'], '/superadmin/')
        c2 = self._client()
        r = c2.post('/account/mfa/verify/', {'code': codes[0], 'next': '/superadmin/'})
        self.assertContains(r, 'الكود غير صحيح')

    def test_api_gets_401_not_a_redirect(self):
        self._enable()
        r = self._client().get('/api/v1/subscription/addon/', HTTP_ACCEPT='application/json')
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json()['error'], 'mfa_required')

    def test_owner_without_mfa_is_nudged_not_blocked(self):
        r = self._client().get('/superadmin/')
        self.assertFalse(r.get('Location', '').startswith('/account/mfa/verify/'))

    def test_server_side_reset_for_a_lost_phone(self):
        self._enable()
        out = StringIO()
        call_command('reset_platform_mfa', self.owner.username, stdout=out)
        self.assertFalse(PlatformUserMFA.objects.get(user=self.owner).is_enabled)
        self.assertTrue(PlatformEvent.objects.filter(description__contains='إعادة ضبط التحقق').exists())
        self.assertFalse(self._client().get('/superadmin/').get('Location', '').startswith('/account/mfa/verify/'))


class RenewalWhatsAppTests(TestCase):

    def test_phone_normalization(self):
        self.assertEqual(normalize_phone('01012345678'), '201012345678')
        self.assertEqual(normalize_phone('+20 101 234 5678'), '201012345678')
        self.assertEqual(normalize_phone('00201012345678'), '201012345678')
        self.assertEqual(normalize_phone('0501234567', 'AE'), '971501234567')
        self.assertIsNone(normalize_phone('123'))

    @override_settings(WHATSAPP_ACCESS_TOKEN='tok', WHATSAPP_PHONE_NUMBER_ID='99',
                       WHATSAPP_RENEWAL_TEMPLATE='subscription_renewal')
    def test_reminder_goes_out_as_the_approved_template_with_the_renew_link(self):
        from types import SimpleNamespace
        from clients.tasks import _send_subscription_warning
        tenant = SimpleNamespace(schema_name='wa_shop', name='ورشة الأمل', email='', phone='01012345678',
                                 country='EG')
        resp = SimpleNamespace(status_code=200, text='{}')
        from django.core.cache import cache
        from django.utils import timezone
        cache.delete(f"dunning_sent:wa_shop:3:{timezone.now().date()}")   # تذكير واحد في اليوم
        with mock.patch('requests.post', return_value=resp) as post, \
             mock.patch('clients.tasks._auto_renew_notice', return_value=''):
            _send_subscription_warning(tenant, days_left=3, is_trial=False)
        self.assertEqual(post.call_count, 1)
        payload = post.call_args.kwargs['json']
        self.assertEqual((payload['to'], payload['template']['name']), ('201012345678', 'subscription_renewal'))
        params = [p['text'] for p in payload['template']['components'][0]['parameters']]
        self.assertEqual(params[0], 'ورشة الأمل')
        self.assertIn('/pricing/?shop=wa_shop&pt=', params[3])
