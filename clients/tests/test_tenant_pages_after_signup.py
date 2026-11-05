"""🧭 بعد التسجيل: الصفحات (قائمة الدخل، الميزان، أعمار الديون…) كانت كلها بتتحوّل
لصفحة «جدّد اشتراكك».

🐛 حارس الاشتراكات (TenantQuotaMiddleware) كان:
  • بيعتبر الشركة اللي لسه مأكّدتش الإيميل (is_active=False) اشتراكها منتهي ⇒ صفحة الأسعار.
  • بيكيّش الحالة 60 ثانية بمفتاح فيه اسم الـ schema الحالية، فالتفعيل من الدومين
    الرئيسي ماكانش بيمسحه ⇒ حتى بعد التفعيل المالك بيفضل يتحوّل للأسعار.
"""
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client as HttpClient, override_settings
from django_tenants.utils import get_tenant_domain_model, schema_context

from clients.models import Client
from inventory.tests.base import ERPTenantTestCase, delete_test_tenant

BASE = {'company_name': 'مطبعة الروابط', 'full_name': 'سعيد محمد', 'email': 'owner@links.test',
        'phone': '01094850763', 'password': 'Str0ng#Pass2026', 'website_url_honeypot': '',
        'subdomain': 'links-print', 'industry': 'printing', 'business_type': 'print_shop'}

SIDEBAR = ['/printing/reports/profit-loss/', '/printing/accounting/', '/printing/accounting/?tab=aging',
           '/printing/accounting/?tab=vat', '/printing/accounting/?tab=periods']


@override_settings(ALLOWED_HOSTS=['*'], SIGNUP_EMAIL_VERIFICATION='required')
class TenantPagesAfterSignupTests(ERPTenantTestCase):

    def setUp(self):
        super().setUp()
        from django.core.cache import cache
        connection.set_schema_to_public()      # مفتاح الكاش فيه اسم الـ schema
        cache.delete('signup_ip:127.0.0.1')
        r = HttpClient().post('/connect/signup/', BASE, HTTP_HOST=self.domain.domain)
        self.assertEqual(r.status_code, 200, r.get('Location'))
        self.shop = Client.all_objects.get(email='owner@links.test')
        host = get_tenant_domain_model().objects.filter(tenant=self.shop).first().domain
        with schema_context(self.shop.schema_name):
            self.owner = get_user_model().objects.get(email='owner@links.test')
        connection.set_schema_to_public()
        self.c = HttpClient(HTTP_HOST=host)

    def tearDown(self):
        connection.set_schema_to_public()
        for t in Client.all_objects.filter(email='owner@links.test'):
            delete_test_tenant(t, None)
        super().tearDown()

    def _login(self):
        with schema_context(self.shop.schema_name):
            self.c.force_login(self.owner)
        connection.set_schema_to_public()

    def test_verify_page_then_pages_open_after_activation_and_pricing_only_when_expired(self):
        # ١) لسه مأكّدش الإيميل ⇒ «أكّد إيميلك» (مش صفحة الأسعار)
        self.assertFalse(self.shop.is_active)
        self._login()
        r = self.c.get('/printing/accounting/')
        self.assertEqual(r.status_code, 403)
        self.assertContains(r, 'لسه مش متفعّل', status_code=403)
        self.assertContains(r, '/account/verify-email/resend/', status_code=403)

        # ٢) اتفعّل ⇒ كل روابط القايمة بتفتح على طول (مفيش حالة قديمة متكيّشة)
        self.shop.is_active = True
        self.shop.save(update_fields=['is_active'])
        for url in SIDEBAR:
            r = self.c.get(url)
            self.assertEqual(r.status_code, 200, f'{url} → {r.status_code} {r.get("Location", "")}')

        # ٣) التجربة خلصت فعلاً ⇒ صفحة الأسعار (السلوك القديم الصح)
        from datetime import timedelta
        from django.utils import timezone
        self.shop.trial_ends_at = timezone.localdate() - timedelta(days=10)
        self.shop.save(update_fields=['trial_ends_at'])
        r = self.c.get('/printing/accounting/')
        self.assertEqual(r.status_code, 302)
        self.assertIn('/pricing/', r['Location'])
