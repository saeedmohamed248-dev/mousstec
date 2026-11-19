"""📣 قياس إعلانات الأسواق + تصليحات صفحات الهبوط.

* حدث «Lead» لما عميل ينشر طلب قطعة (السيرفر + نفس الـ id للمتصفح).
* حدث «SubmitApplication» لما مطبعة تتسجل في سوق المطابع (بيتبعت مرة واحدة بعد الـ redirect).
* بيانات عميل السوق بتتبعت لفيسبوك مشفّرة، ومفيش إرسال لو الـ CAPI مش متظبط.
* سطر «تصميم AI» مايتكررش في باقات المطابع، وصفحة السيارات فيها الأسعار.
"""
import hashlib
from contextlib import nullcontext
from types import SimpleNamespace
from unittest import mock

from django.core.cache import cache
from django.test import Client as DjangoClient
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils.html import escapejs

from clients.services import meta_capi
from clients.tests.test_printing_customer_flow import _TenantDomainMixin
from marketplace_c2c.tests.test_parts_order_lifecycle import (
    _customer,
    _login,
    _make,
    _PublicDomainMixin,
)

META = dict(META_PIXEL_ID='1234567890', META_CAPI_TOKEN='tok', META_CAPI_TEST_CODE='')
PW = 'Strong-Pass-2026'


def _sha(v):
    return hashlib.sha256(v.encode()).hexdigest()


class ContactPayloadTests(SimpleTestCase):

    def test_market_customer_data_is_hashed_with_browser_ids(self):
        req = SimpleNamespace(COOKIES={'_fbp': 'fb.1.1.123', '_fbc': 'fb.1.1.IwAR9'},
                              META={'HTTP_X_FORWARDED_FOR': '41.1.2.3, 10.0.0.1', 'HTTP_USER_AGENT': 'UA'})
        data = meta_capi.contact_user_data(email='Buyer@Mail.com', phone='01012345678',
                                           external_id='mpc-5', request=req)
        self.assertEqual(data['em'], [_sha('buyer@mail.com')])
        self.assertEqual(data['ph'], [_sha('201012345678')])
        self.assertEqual(data['external_id'], [_sha('mpc-5')])
        self.assertEqual((data['fbp'], data['fbc'], data['client_ip_address']),
                         ('fb.1.1.123', 'fb.1.1.IwAR9', '41.1.2.3'))
        self.assertNotIn('Buyer@Mail.com', str(data))

    @override_settings(META_PIXEL_ID='', META_CAPI_TOKEN='')
    def test_nothing_sent_when_not_configured(self):
        self.assertFalse(meta_capi.send_contact('Lead', event_id='x', email='a@b.com'))


class LandingPlansTests(SimpleTestCase):
    """🐛 «50 تصميم AI/شهر» كان بيظهر مرتين في كارت الباقة."""

    def _rows(self, plans):
        from clients.templatetags.landing_tags import landing_plans
        qs = mock.Mock()
        qs.order_by.return_value = plans
        with mock.patch('clients.models.Plan.objects.filter', return_value=qs), \
                mock.patch('django_tenants.utils.schema_context', return_value=nullcontext()):
            return landing_plans({'request': None}, 'printing')

    def _plan(self, features, quota=50):
        return SimpleNamespace(name='ستارتر', name_en='Starter', slug='starter', monthly_price=875,
                               monthly_price_aed=0, max_users=2, max_branches=1, max_treasuries=1,
                               monthly_ai_designs_quota=quota, features=features)

    def test_ai_line_not_duplicated_when_already_in_features(self):
        with mock.patch('clients.services.plan_compare.plan_display_name', return_value='ستارتر'):
            row = self._rows([self._plan(['طلبات غير محدودة', '50 تصميم AI/شهر'])])[0]
            self.assertIsNone(row['ai_quota'])
            self.assertEqual(row['features'].count('50 تصميم AI/شهر'), 1)
            row = self._rows([self._plan(['طلبات غير محدودة'])])[0]
            self.assertEqual(row['ai_quota'], 50)


@override_settings(ALLOWED_HOSTS=['*'], **META)
class PartsWantedLeadTests(_PublicDomainMixin, TestCase):

    def test_posting_request_sends_lead_with_same_id_for_browser(self):
        make, buyer = _make(), _customer()
        with mock.patch.object(meta_capi, 'send_contact') as send:
            res = _login(buyer).post('/marketplace/parts/wanted/new/', {
                'car_make': make.pk, 'car_model': 'X5', 'car_year': '2014', 'part_name': 'Compressor'})
        self.assertEqual(res.status_code, 200, res.content)
        event_id = res.json()['pixel_event_id']
        self.assertTrue(event_id.startswith('pw-'))
        send.assert_called_once()
        self.assertEqual(send.call_args.args[0], 'Lead')
        self.assertEqual(send.call_args.kwargs['event_id'], event_id)
        self.assertEqual(send.call_args.kwargs['external_id'], f'mpc-{buyer.pk}')

    def test_logged_out_visitor_lands_on_signup_not_login(self):
        res = DjangoClient().get('/marketplace/parts/wanted/new/')
        self.assertEqual(res.status_code, 302)
        self.assertIn('/marketplace/automotive/?next=/marketplace/parts/wanted/new/', res['Location'])
        self.assertNotIn('tab=login', res['Location'])

    def test_parts_pages_load_the_pixel(self):
        res = DjangoClient().get('/marketplace/parts/')
        self.assertContains(res, "fbq('init', '1234567890')")


@override_settings(ALLOWED_HOSTS=['*'], **META)
class PrintShopSignupEventTests(_TenantDomainMixin, TestCase):

    def setUp(self):
        cache.clear()

    def test_shop_signup_fires_submit_application_once(self):
        c = DjangoClient()
        with mock.patch.object(meta_capi, 'send_contact') as send:
            r = c.post(reverse('print_market:signup'), {'role': 'shop', 'name': 'مطبعة النور',
                                                        'email': 'shop@x.com', 'password': PW,
                                                        'city': 'القاهرة'})
        self.assertEqual(r.status_code, 302, r.content[:300])
        send.assert_called_once()
        self.assertEqual(send.call_args.args[0], 'SubmitApplication')
        event_id = send.call_args.kwargs['event_id']
        page = c.get(r['Location'])
        self.assertContains(page, "fbq('track', 'SubmitApplication'")
        # 🐛 [FIX]: القالب بيعدّي الـ id على escapejs فالشرطة بتطلع \u002D (نفس القيمة في الـ JS)
        self.assertContains(page, escapejs(event_id))
        # مرة واحدة بس — الصفحة اللي بعدها مافيهاش الحدث
        self.assertNotContains(c.get(r['Location']), "fbq('track', 'SubmitApplication'")

    def test_customer_signup_does_not_fire_shop_event(self):
        with mock.patch.object(meta_capi, 'send_contact') as send:
            DjangoClient().post(reverse('print_market:signup'), {'role': 'customer', 'name': 'أحمد',
                                                                 'email': 'c@x.com', 'password': PW})
        send.assert_not_called()


@override_settings(ALLOWED_HOSTS=['*'], **META)
class CatalogQuoteLeadTests(_TenantDomainMixin, TestCase):
    """📣 حملة «مطبوعات الشركات»: طلب سعر من كتالوج الطباعة ⇒ «Lead» (سيرفر + متصفح بنفس الـ id، مرة واحدة)."""

    def setUp(self):
        from clients.catalog_seed import seed_catalog
        from clients.models import CatalogItem, CatalogStore
        cache.clear()
        if not CatalogStore.objects.exists():
            seed_catalog(CatalogStore, CatalogItem)
        self.item = CatalogItem.objects.filter(store__sector='printing').first()

    def test_quote_request_fires_lead_once_with_same_id(self):
        from django.template.defaultfilters import escapejs
        from clients.models import CatalogQuoteRequest
        c = DjangoClient()
        url = f'/marketplace/catalog/item/{self.item.item_code}/'
        self.assertContains(c.get(url), "fbq('init', '1234567890')")
        with mock.patch.object(meta_capi, 'send_contact') as send:
            r = c.post(url, {'name': 'شركة النور', 'phone': '01011112222', 'quantity': '٥٠٠'})
        self.assertEqual(r.status_code, 302)
        q = CatalogQuoteRequest.objects.get()
        send.assert_called_once()
        self.assertEqual(send.call_args.args[0], 'Lead')
        event_id = send.call_args.kwargs['event_id']
        self.assertEqual(event_id, f'cq-{q.request_code}')
        page = c.get(r['Location'])
        self.assertContains(page, "fbq('track', 'Lead'")
        self.assertContains(page, escapejs(event_id))
        self.assertNotContains(c.get(url), "fbq('track', 'Lead'")

    def test_invalid_request_sends_nothing(self):
        with mock.patch.object(meta_capi, 'send_contact') as send:
            r = DjangoClient().post(f'/marketplace/catalog/item/{self.item.item_code}/', {'name': 'م', 'phone': '1'})
        self.assertEqual(r.status_code, 200)
        send.assert_not_called()
