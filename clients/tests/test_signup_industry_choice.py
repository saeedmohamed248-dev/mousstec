"""🏭 المجال في التسجيل لازم يتختار — مايتعملش «سيارات» لوحده.

🐛 كارت «سيارات» كان مختار افتراضياً، فاللي بيسجّل مطبعة ومايضغطش على كارت
«طباعة وتصميم» كان بيتعمله حساب قطع غيار من غير ما ياخد باله.
"""
from django.test import Client as HttpClient, SimpleTestCase, override_settings

from clients.forms import TenantSignupForm
from clients.models import Client
from inventory.tests.base import ERPTenantTestCase

BASE = {
    'company_name': 'مطبعة الاختيار', 'full_name': 'سعيد محمد', 'email': 'owner@choice.test',
    'phone': '01000000000', 'password': 'Str0ng#Pass2026', 'website_url_honeypot': '', 'subdomain': '',
}


class SignupIndustryFormTests(SimpleTestCase):

    def form(self, **kw):
        return TenantSignupForm(data=dict(BASE, **kw))

    def test_industry_must_be_chosen(self):
        form = self.form(industry='', business_type='')
        self.assertFalse(form.is_valid())
        self.assertIn('industry', form.errors)

    def test_business_type_must_belong_to_the_industry(self):
        form = self.form(industry='printing', business_type='service_center')
        self.assertFalse(form.is_valid())
        self.assertIn('business_type', form.errors)

    def test_printing_signup_is_valid(self):
        form = self.form(industry='printing', business_type='print_shop')
        self.assertTrue(form.is_valid(), form.errors)


@override_settings(ALLOWED_HOSTS=['*'])
class SignupIndustryPageTests(ERPTenantTestCase):

    def setUp(self):
        from django.core.cache import cache
        cache.delete('signup_ip:127.0.0.1')
        self.c = HttpClient()

    def get(self, url):
        return self.c.get(url, HTTP_HOST=self.domain.domain).content.decode()

    def test_no_industry_is_preselected_without_a_link_hint(self):
        html = self.get('/connect/signup/')
        self.assertNotIn('industry-card active', html)
        self.assertIn('<option value="" selected>', html)
        self.assertIn('id="industryHint"', html)

    def test_industry_link_preselects_it(self):
        html = self.get('/connect/signup/?industry=printing')
        self.assertIn('<option value="printing" selected>', html)
        self.assertIn('<option value="print_shop" selected>', html)

    def test_post_without_industry_creates_nothing(self):
        before = Client.all_objects.count()
        r = self.c.post('/connect/signup/', dict(BASE, industry='', business_type='', subdomain='choice-shop'),
                        HTTP_HOST=self.domain.domain)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'اختار مجالك الأول')
        self.assertEqual(Client.all_objects.count(), before)
