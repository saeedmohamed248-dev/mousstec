"""📣 قياس الإعلانات: مصدر كل شركة + أحداث فيسبوك (Pixel + Conversions API) + صفحة «الإعلانات».

قبل كده مفيش أي قياس: لا Pixel ولا UTM ولا أي حاجة بتقول الشركة جت منين — أي فلوس إعلانات كانت
هتتصرف من غير ما نعرف أنهي إعلان جاب تسجيل وأنهي جاب اشتراك مدفوع.
"""
import hashlib
import os
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.core import signing
from django.db import connection
from django.test import Client as HttpClient, SimpleTestCase, TestCase, override_settings

from clients.models import Client, Domain, Plan, PlatformInvoice, SignupAttribution, TenantSubscription
from clients.services import attribution, meta_capi
from clients.services import platform_billing as pb
from inventory.tests.base import ERPTenantTestCase
from marketplace_c2c.tests.test_parts_order_lifecycle import _PublicDomainMixin

User = get_user_model()
AD_QUERY = {'utm_source': 'facebook', 'utm_medium': 'paid', 'utm_campaign': 'workshops_cairo',
            'utm_content': 'video_profit', 'fbclid': 'IwAR123'}
META = dict(META_PIXEL_ID='1234567890', META_CAPI_TOKEN='tok', META_CAPI_TEST_CODE='')


def _sha(v):
    return hashlib.sha256(v.encode()).hexdigest()


@override_settings(ALLOWED_HOSTS=['*'], BASE_DOMAIN='mousstec.com')
class AttributionCookieTests(_PublicDomainMixin, TestCase):

    def test_ad_link_stores_campaign_for_main_domain_and_subdomains(self):
        r = HttpClient().get('/pricing/', AD_QUERY, HTTP_HOST='auto.mousstec.com')
        cookie = r.cookies[attribution.COOKIE]
        self.assertEqual(cookie['domain'], '.mousstec.com')
        self.assertTrue(cookie['httponly'])
        data = signing.loads(cookie.value, salt='mt-attribution')
        self.assertEqual((data['source'], data['campaign'], data['content']),
                         ('facebook', 'workshops_cairo', 'video_profit'))
        self.assertTrue(data['fbc'].startswith('fb.1.') and data['fbc'].endswith('.IwAR123'))

    def test_plain_visits_do_not_touch_the_campaign_and_tampered_cookies_are_ignored(self):
        r = HttpClient().get('/pricing/')
        self.assertNotIn(attribution.COOKIE, r.cookies)
        req = mock.Mock(COOKIES={attribution.COOKIE: 'forged'})
        self.assertEqual(attribution.read(req), {})


class MetaCapiPayloadTests(SimpleTestCase):

    def test_customer_data_is_hashed_and_phone_has_country_code(self):
        self.assertEqual(meta_capi.normalize_phone('010-1234-5678'), '201012345678')
        self.assertEqual(meta_capi.normalize_phone('0501234567', 'AE'), '971501234567')
        self.assertEqual(meta_capi.normalize_phone('+20 101 234 5678'), '201012345678')
        tenant = Client(pk=7, email=' Owner@Shop.COM ', phone='01012345678', country='EG')
        with mock.patch.object(Client, 'attribution', None, create=True):
            event = meta_capi.build_event('Purchase', tenant, event_id='inv-1', value=Decimal('550'), currency='EGP')
        self.assertEqual(event['user_data']['em'], [_sha('owner@shop.com')])
        self.assertEqual(event['user_data']['ph'], [_sha('201012345678')])
        self.assertEqual(event['user_data']['external_id'], [_sha('tenant-7')])
        self.assertEqual(event['custom_data'], {'value': 550.0, 'currency': 'EGP'})
        self.assertNotIn('Owner@Shop.COM', str(event))

    @override_settings(META_PIXEL_ID='', META_CAPI_TOKEN='')
    def test_nothing_is_sent_when_not_configured(self):
        self.assertFalse(meta_capi.send('Purchase', Client(pk=1), event_id='x'))


class _SyncThread:
    def __init__(self, target, args=(), daemon=None):
        self.target, self.args = target, args

    def start(self):
        self.target(*self.args)


@override_settings(ALLOWED_HOSTS=['*'], **META)
class PurchaseEventTests(ERPTenantTestCase):

    def setUp(self):
        super().setUp()
        connection.set_schema_to_public()
        self.plan = Plan.objects.update_or_create(slug='auto-silver', defaults=dict(
            name='Silver', industry='automotive', monthly_price=Decimal('550'), max_users=5,
            max_branches=5, max_treasuries=5, is_active=True))[0]
        self.t = type(self.tenant).objects.get(pk=self.tenant.pk)
        type(self.tenant).objects.filter(pk=self.t.pk).update(email='owner@shop.test', phone='01011112222',
                                                              country='EG')
        self.t.refresh_from_db()
        SignupAttribution.objects.update_or_create(tenant=self.t, defaults=dict(
            source='facebook', campaign='workshops_cairo', fbc='fb.1.1.IwAR1', fbp='fb.1.2.3',
            ip='41.1.2.3', user_agent='Mozilla'))

    def tearDown(self):
        connection.set_schema_to_public()
        PlatformInvoice.objects.filter(tenant_id=self.t.pk).delete()
        TenantSubscription.objects.filter(tenant_id=self.t.pk).delete()
        SignupAttribution.objects.filter(tenant_id=self.t.pk).delete()
        connection.set_tenant(self.tenant)
        super().tearDown()

    def test_real_payment_sends_purchase_with_value_and_ad_click(self):
        # ERPTenantTestCase بيعمل commit فعلي ⇒ on_commit بيشتغل بعد الدفع زي الإنتاج
        with mock.patch('clients.services.meta_capi.requests.post',
                           return_value=mock.Mock(status_code=200, text='')) as post, \
                mock.patch('clients.services.meta_capi.threading.Thread', _SyncThread):
            inv = pb.issue_subscription_invoice(self.t, self.plan, 1, provider='paymob', reference='o-1')
        post.assert_called_once()
        url, payload = post.call_args.args[0], post.call_args.kwargs['json']
        self.assertIn('/1234567890/events', url)
        event = payload['data'][0]
        self.assertEqual((event['event_name'], event['event_id']), ('Purchase', f'inv-{inv.invoice_number}'))
        self.assertEqual(event['custom_data'], {'value': float(inv.total), 'currency': 'EGP'})
        self.assertEqual((event['user_data']['fbc'], event['user_data']['client_ip_address']),
                         ('fb.1.1.IwAR1', '41.1.2.3'))
        self.assertEqual(event['user_data']['em'], [_sha('owner@shop.test')])

    def test_gifts_send_nothing(self):
        with mock.patch('clients.services.meta_capi.send') as send:
            pb.issue_subscription_invoice(self.t, self.plan, 1, gift=True)
        send.assert_not_called()


@override_settings(ALLOWED_HOSTS=['*'], EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
                   SIGNUP_EMAIL_VERIFICATION='required', **META)
class SignupAttributionTests(ERPTenantTestCase):
    FORM = {
        'company_name': 'ورشة الإعلان', 'industry': 'automotive', 'business_type': 'service_center',
        'full_name': 'سعيد محمد', 'email': 'owner@ads.test', 'phone': '01000000000',
        'password': 'Str0ng#Pass2026', 'website_url_honeypot': '', 'subdomain': 'ads-shop',
    }

    def setUp(self):
        self.env = mock.patch.dict(os.environ, {'BASE_DOMAIN': 'test.com'})
        self.env.start()
        from django.core.cache import cache
        cache.delete('signup_ip:127.0.0.1')
        self.c = HttpClient()

    def tearDown(self):
        self.env.stop()
        connection.set_schema_to_public()
        for t in Client.all_objects.filter(schema_name__startswith='ads_shop'):
            TenantSubscription.objects.filter(tenant=t).delete()
            SignupAttribution.objects.filter(tenant=t).delete()
            Domain.objects.filter(tenant=t).delete()
            t.delete(force_drop=True)
        connection.set_tenant(self.tenant)
        super().tearDown()

    def test_signup_from_an_ad_keeps_its_campaign_and_reports_the_registration(self):
        self.c.get('/', AD_QUERY, HTTP_HOST=self.domain.domain)              # ضغط على الإعلان
        self.c.cookies['_fbp'] = 'fb.1.111.222'
        with mock.patch('clients.services.meta_capi.send') as send:
            r = self.c.post('/connect/signup/', self.FORM, HTTP_HOST=self.domain.domain,
                            HTTP_USER_AGENT='Mozilla/5.0 test')
        self.assertEqual(r.status_code, 200)
        connection.set_schema_to_public()
        tenant = Client.all_objects.get(schema_name='ads_shop')
        a = SignupAttribution.objects.get(tenant=tenant)
        self.assertEqual((a.source, a.medium, a.campaign, a.content), ('facebook', 'paid', 'workshops_cairo',
                                                                       'video_profit'))
        self.assertEqual((a.fbp, a.user_agent), ('fb.1.111.222', 'Mozilla/5.0 test'))
        self.assertTrue(a.fbc.endswith('.IwAR123'))
        send.assert_called_once()
        self.assertEqual(send.call_args.args[0], 'CompleteRegistration')
        self.assertEqual(send.call_args.kwargs['event_id'], f'reg-{tenant.pk}')
        # نفس الـ event_id في Pixel المتصفح ⇒ فيسبوك بيحسب التسجيل مرة واحدة
        self.assertContains(r, f"eventID: 'reg\\u002D{tenant.pk}'")   # escapejs ⇒ نفس 'reg-<id>' في JS
        self.assertContains(r, "fbq('init', '1234567890')")


@override_settings(ALLOWED_HOSTS=['*'])
class PixelAndMarketingPageTests(_PublicDomainMixin, TestCase):

    def test_pixel_only_when_configured(self):
        with override_settings(META_PIXEL_ID=''):
            self.assertNotContains(HttpClient().get('/features/'), 'fbevents.js')
        with override_settings(META_PIXEL_ID='999'):
            r = HttpClient().get('/pricing/')
            self.assertContains(r, "fbq('init', '999')")
            self.assertContains(r, "fbq('track', 'ViewContent')")

    def test_owner_sees_which_campaign_brought_paying_companies(self):
        plan = Plan.objects.update_or_create(slug='auto-silver', defaults=dict(
            name='Silver', industry='automotive', monthly_price=Decimal('550'), is_active=True))[0]

        def shop(schema, **attr):
            t = Client(name=schema, owner_name='x', phone='0100', schema_name=schema, is_active=True)
            t.auto_create_schema = False
            t.save()
            if attr:
                SignupAttribution.objects.create(tenant=t, **attr)
            return t

        a = shop('ad_a', source='facebook', campaign='workshops_cairo', content='video_profit')
        shop('ad_b', source='facebook', campaign='workshops_cairo', content='video_profit')
        shop('organic')
        sub = TenantSubscription.objects.create(tenant=a, plan=plan)
        rev = pb._revision_for(plan)
        from django.utils import timezone
        today = timezone.localdate()
        for provider, total in (('paymob', Decimal('550')), ('gift', Decimal('0'))):
            PlatformInvoice.objects.create(tenant=a, subscription=sub, plan_revision=rev, period_start=today,
                                           period_end=today, billing_cycle_months=1, subtotal=total,
                                           total=total, status='paid', payment_provider=provider)
        owner = User.objects.create_user('owner@plat.test', password='x', is_superuser=True, is_staff=True)
        c = HttpClient()
        c.force_login(owner)
        r = c.get('/superadmin/marketing/')
        self.assertEqual(r.status_code, 200)
        rows = {(g['source'], g['campaign']): g for g in r.context['rows']}
        fb = rows[('facebook', 'workshops_cairo')]
        self.assertEqual((fb['signups'], fb['paid'], fb['revenue'], fb['conversion']),
                         (2, 1, Decimal('550'), Decimal('50.0')))
        self.assertEqual(rows[('direct', '—')]['paid'], 0)
        self.assertContains(r, 'utm_campaign')                 # أداة عمل لينك الإعلان

    def test_marketing_page_is_for_platform_staff_only(self):
        c = HttpClient()
        c.force_login(User.objects.create_user('nobody@x.test', password='x'))
        self.assertNotEqual(c.get('/superadmin/marketing/').status_code, 200)
