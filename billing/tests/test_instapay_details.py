"""🏦 عنوان إنستاباي المنصة — من الإعدادات في كل صفحات الدفع.

🐛 قبل كده الصفحات كانت بتعرض «01094850763@instapay» (مش عنوان المالك)، فاللي يختار إنستاباي
يحوّل لعنوان غلط. دلوقتي العنوان + لينك الدفع (ipn.eg) من INSTAPAY_HANDLE / INSTAPAY_LINK.
"""
from django.conf import settings
from django.test import Client as HttpClient, TestCase, override_settings

from billing.tests.test_promotions_and_plan_admin import _plan
from clients.services.pricing_facts import payment_methods_text
from marketplace_c2c.tests.test_parts_order_lifecycle import _PublicDomainMixin

OLD = '01094850763@instapay'


@override_settings(ALLOWED_HOSTS=['*'])
class InstapayDetailsTests(_PublicDomainMixin, TestCase):

    def test_defaults_are_the_owner_account(self):
        self.assertEqual(settings.INSTAPAY_HANDLE, 'saeed.mohamed.123@instapay')
        self.assertTrue(settings.INSTAPAY_LINK.startswith('https://ipn.eg/'))

    def test_pricing_payment_box_shows_the_handle_and_the_app_link(self):
        _plan('auto-gold', '800')
        html = HttpClient().get('/pricing/').content.decode()
        self.assertIn(settings.INSTAPAY_HANDLE, html)
        self.assertIn(f'href="{settings.INSTAPAY_LINK}"', html)
        self.assertNotIn(OLD, html)

    @override_settings(INSTAPAY_HANDLE='shop.x@instapay', INSTAPAY_LINK='https://ipn.eg/S/shop.x/instapay/abc')
    def test_changing_the_setting_changes_every_page(self):
        _plan('auto-gold', '800')
        html = HttpClient().get('/pricing/').content.decode()
        self.assertIn('shop.x@instapay', html)
        self.assertIn('https://ipn.eg/S/shop.x/instapay/abc', html)

    def test_assistant_answers_with_the_real_handle(self):
        text = payment_methods_text({'card_payments': False})
        self.assertIn(settings.INSTAPAY_HANDLE, text)
        self.assertNotIn(OLD, text)
