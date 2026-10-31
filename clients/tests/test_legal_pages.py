"""⚖️ سياسة الخصوصية والشروط — كانوا مش موجودين خالص (مطلوبين قانونياً ومن Paymob وMeta)."""
from django.test import Client as HttpClient, TestCase, override_settings

from marketplace_c2c.tests.test_parts_order_lifecycle import _PublicDomainMixin


@override_settings(ALLOWED_HOSTS=['*'])
class LegalPagesTests(_PublicDomainMixin, TestCase):

    def test_privacy_and_terms_in_arabic_and_english(self):
        c = HttpClient()
        r = c.get('/privacy/')
        self.assertContains(r, 'سياسة الخصوصية')
        self.assertContains(r, '151 لسنة 2020')
        self.assertContains(r, 'Paymob')
        self.assertContains(c.get('/terms/'), 'التجربة المجانية')
        r = c.get('/privacy/?lang=en')
        self.assertContains(r, 'Privacy Policy')
        self.assertContains(r, 'lang="en"')
        self.assertContains(c.get('/terms/?lang=en'), 'Terms of Service')

    def test_signup_and_pricing_link_to_them(self):
        c = HttpClient()
        for url in ('/connect/signup/', '/pricing/'):
            body = c.get(url).content.decode()
            self.assertIn('/privacy/', body, url)
            self.assertIn('/terms/', body, url)
