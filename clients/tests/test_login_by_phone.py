"""📱 الدخول من الصفحة الموحّدة (/login/) برقم موبايل الشركة + كلمة السر.

🐛 الخانة مكتوب فيها «example@email.com أو 01xxxxxxxxx» بس الموبايل + كلمة السر كان بيرد
«لا يوجد حساب بهذا البريد» — الرقم متسجّل على الشركة مش على المستخدم.
"""
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.db import connection
from django.test import Client, SimpleTestCase, override_settings
from django_tenants.utils import schema_context

from clients.views.auth_views import _phone_variants
from inventory.tests.base import ERPTenantTestCase


@override_settings(ALLOWED_HOSTS=['*'])
class LoginByPhoneTests(ERPTenantTestCase):
    PUBLIC_HOST = 'phone-login-public.test'

    def setUp(self):
        super().setUp()
        cache.clear()
        # 🌐 دومين عام (TransactionTestCase بيمسح الجداول بين الاختبارات ⇒ بيتعمل كل مرة)
        from django_tenants.utils import get_tenant_domain_model, get_tenant_model
        connection.set_schema_to_public()
        Tenant, Domain = get_tenant_model(), get_tenant_domain_model()
        public = Tenant.objects.filter(schema_name='public').first()
        if public is None:
            public = Tenant(schema_name='public', name='Public', owner_name='T', phone='000')
            public.auto_create_schema = False
            public.save(verbosity=0)
        Domain.objects.get_or_create(domain=self.PUBLIC_HOST, defaults={'tenant': public, 'is_primary': False})
        type(self.tenant).objects.filter(pk=self.tenant.pk).update(
            phone='01012345678', email='phone_owner@x.com', status='trial', is_active=True)
        with schema_context(self.tenant.schema_name):
            u, _ = User.objects.get_or_create(username='phone_owner@x.com', defaults={'email': 'phone_owner@x.com'})
            u.set_password('Good#Pass2026')
            u.save()
        connection.set_schema_to_public()

    def _login(self, ident, pw):
        return Client().post('/login/', {'email': ident, 'password': pw}, HTTP_HOST=self.PUBLIC_HOST)

    def test_login_by_phone_email_and_wrong_password(self):
        # اختبار واحد: TransactionTestCase بيمسح جداول public بين الاختبارات (صف الشركة نفسه)
        for ident in ('01012345678', '+20 101 234 5678', '201012345678', 'phone_owner@x.com'):
            cache.clear()
            r = self._login(ident, 'Good#Pass2026')
            self.assertEqual(r.status_code, 302, ident)
            self.assertIn('/auto-login/?token=', r['Location'])
        cache.clear()
        r = self._login('01012345678', 'nope-nope')
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'لا يوجد حساب')

        # 🐛 من غير إرسال إيميلات: اللي قفل صفحة التسجيل قبل رابط التفعيل كان بيتقفل برا حسابه
        type(self.tenant).objects.filter(pk=self.tenant.pk).update(is_active=False)
        cache.clear()
        with mock.patch('clients.services.email_verification.verification_required', return_value=False):
            r = self._login('phone_owner@x.com', 'Good#Pass2026')
        self.assertEqual(r.status_code, 302)
        self.assertTrue(type(self.tenant).objects.get(pk=self.tenant.pk).is_active)
        # لو الإيميل متظبط: لازم الرابط زي الأول
        type(self.tenant).objects.filter(pk=self.tenant.pk).update(is_active=False)
        cache.clear()
        with mock.patch('clients.services.email_verification.verification_required', return_value=True):
            r = self._login('phone_owner@x.com', 'Good#Pass2026')
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'مش متفعّل')


class PhoneVariantsTests(SimpleTestCase):

    def test_variants_cover_local_and_international(self):
        v = _phone_variants('+20 101 234 5678')
        self.assertIn('01012345678', v)
        self.assertIn('201012345678', v)
        self.assertEqual(_phone_variants('a@b.com'), [])
