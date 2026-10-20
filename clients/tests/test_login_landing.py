"""وجهة المستخدم بعد الدخول الموحّد (/login/ ← /auto-login/) حسب قطاع الشركة.

🐛 الدخول الموحّد كان بيودّي الكل على /system/dashboard/ (قسم السيارات)، فصاحب
المطبعة كان بيدخل على «404 — هذا القسم غير متوفر لحسابك» بعد كل تسجيل دخول.
"""
import time

from django.contrib.auth.models import User
from django.core import signing
from django.db import connection
from django.test import Client, override_settings

from inventory.tests.base import ERPTenantTestCase


@override_settings(ALLOWED_HOSTS=['*'])
class LoginLandingByIndustryTests(ERPTenantTestCase):

    def setUp(self):
        super().setUp()
        connection.set_tenant(self.tenant)
        self.host = self.domain.domain
        self.owner = User.objects.create_user('landing_owner', password='x', is_staff=True,
                                              is_superuser=True)

    def _auto_login(self, nxt=''):
        token = signing.dumps({'schema_name': self.tenant.schema_name, 'user_id': self.owner.pk,
                               'created': int(time.time()), 'next': nxt}, salt='tenant-auto-login-token')
        c = Client()
        return c, c.get(f'/auto-login/?token={token}', HTTP_HOST=self.host)

    def _set_industry(self, industry):
        type(self.tenant).objects.filter(pk=self.tenant.pk).update(industry=industry)

    def test_printing_owner_lands_on_the_printing_admin_not_a_404(self):
        self._set_industry('printing')
        c, r = self._auto_login()
        self.assertEqual(r['Location'], '/secure-portal/')
        self.assertEqual(c.get('/secure-portal/', HTTP_HOST=self.host).status_code, 200)
        r = c.get('/auth/redirect/', HTTP_HOST=self.host)
        self.assertEqual(r['Location'], '/secure-portal/')

    def test_automotive_owner_still_lands_on_the_dashboard(self):
        c, r = self._auto_login()
        self.assertEqual(r['Location'], '/system/dashboard/')
        self.assertEqual(c.get('/system/dashboard/', HTTP_HOST=self.host).status_code, 200)

    def test_next_is_still_honoured_and_unsafe_next_falls_back_per_industry(self):
        self._set_industry('printing')
        self.assertEqual(self._auto_login('/hr/manager/')[1]['Location'], '/hr/manager/')
        self.assertEqual(self._auto_login('//evil.com')[1]['Location'], '/secure-portal/')
