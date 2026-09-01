"""
اختبارات مناطق المنصة — تضمن إن الموقع الإماراتي يُكتشف صح ويعطي AED،
والمصري (والافتراضي) يعطي EGP.
"""
from decimal import Decimal

from django.test import SimpleTestCase, override_settings

from erp_core.regions import region_country_for_host, resolve_region, region_links


class _FakeReq:
    def __init__(self, host, path='/pricing/', secure=True):
        self._host, self._path, self._secure = host, path, secure

    def get_host(self):
        return self._host

    def get_full_path(self):
        return self._path

    def is_secure(self):
        return self._secure


@override_settings(BASE_DOMAIN='mousstec.com', REGION_AE_HOSTS=['ae.mousstec.com'],
                   DEFAULT_REGION_COUNTRY='EG')
class RegionResolutionTests(SimpleTestCase):
    def test_ae_subdomain_is_uae(self):
        self.assertEqual(region_country_for_host('ae.mousstec.com'), 'AE')

    def test_ae_with_port(self):
        self.assertEqual(region_country_for_host('ae.mousstec.com:443'), 'AE')

    def test_base_domain_is_egypt(self):
        self.assertEqual(region_country_for_host('mousstec.com'), 'EG')

    def test_tenant_subdomain_defaults_to_egypt(self):
        self.assertEqual(region_country_for_host('shop1.mousstec.com'), 'EG')

    def test_first_label_ae_is_uae(self):
        # أي هوست يبدأ بـ ae. يُعامل كإمارات (مرونة)
        self.assertEqual(region_country_for_host('ae.example.org'), 'AE')

    def test_empty_host_is_default(self):
        self.assertEqual(region_country_for_host(''), 'EG')
        self.assertEqual(region_country_for_host(None), 'EG')

    def test_resolve_region_ae_payload(self):
        r = resolve_region('ae.mousstec.com')
        self.assertEqual(r['country'], 'AE')
        self.assertEqual(r['currency'], 'AED')
        self.assertEqual(r['currency_symbol'], 'د.إ')
        self.assertEqual(r['vat_rate'], Decimal('5.00'))

    def test_resolve_region_eg_payload(self):
        r = resolve_region('mousstec.com')
        self.assertEqual(r['country'], 'EG')
        self.assertEqual(r['currency'], 'EGP')
        self.assertEqual(r['currency_symbol'], 'ج.م')
        self.assertEqual(r['vat_rate'], Decimal('14.00'))


@override_settings(BASE_DOMAIN='mousstec.com',
                   REGION_AE_HOSTS=['ae.mousstec.com', 'mousstec.ae'],
                   DEFAULT_REGION_COUNTRY='EG')
class MultiHostRegionTests(SimpleTestCase):
    def test_multiple_ae_hosts(self):
        self.assertEqual(region_country_for_host('mousstec.ae'), 'AE')
        self.assertEqual(region_country_for_host('ae.mousstec.com'), 'AE')


@override_settings(BASE_DOMAIN='mousstec.com', REGION_AE_HOSTS=['ae.mousstec.com'],
                   DEFAULT_REGION_COUNTRY='EG')
class RegionLinksTests(SimpleTestCase):
    def test_links_preserve_path_and_mark_current(self):
        links = region_links(_FakeReq('ae.mousstec.com', '/pricing/'))
        eg = next(l for l in links if l['country'] == 'EG')
        ae = next(l for l in links if l['country'] == 'AE')
        self.assertEqual(eg['url'], 'https://mousstec.com/pricing/')
        self.assertEqual(ae['url'], 'https://ae.mousstec.com/pricing/')
        self.assertTrue(ae['is_current'])
        self.assertFalse(eg['is_current'])

    def test_current_flag_on_egypt_when_on_base(self):
        links = region_links(_FakeReq('mousstec.com', '/'))
        eg = next(l for l in links if l['country'] == 'EG')
        self.assertTrue(eg['is_current'])
