"""
🧹 «عميل جديد دخل ولقى ١٠ أخطاء» — صفحة 360° للشركة كانت بتعرض 404/403 من غير المسار
ولا مين، وبتحسبهم كلهم «أخطاء غير محلولة».

اللي كان بيحصل: آيفون/بوتات معاينة اللينكات بيطلبوا `/apple-touch-icon.png` و`/favicon.png`
من نفسهم، وبوتات الفحص بتجرّب `/.env` و`/api/...` على أي دومين جديد ⇒ 404/403 بتتسجّل
على الشركة، والعميل نفسه ماشافش أي مشكلة.

* الأيقونات دي بتتحوّل لأيقونة المنصة (مش 404 ومش بتتسجّل).
* 360° بيعرض المسار + المستخدم/«زائر»، وضوضاء الزوار بتتعد لوحدها مش في الرقم الأحمر.
* X-Forwarded-For مش IP كان بيوقّع تسجيل الخطأ نفسه (عمود inet) فالخطأ بيضيع.
"""
from django.contrib.auth.models import User
from django.db import connection
from django.test import Client as HttpClient, override_settings

from clients.models import Client, Domain, SystemErrorLog
from .base import ERPTenantTestCase

PLATFORM_HOST = 'platform-noise.test.com'


@override_settings(ALLOWED_HOSTS=['*'])
class TenantErrorNoiseTests(ERPTenantTestCase):
    def setUp(self):
        connection.set_schema_to_public()
        SystemErrorLog.objects.filter(tenant_schema=self.tenant.schema_name).delete()
        connection.set_tenant(self.tenant)

    def tearDown(self):
        connection.set_schema_to_public()
        SystemErrorLog.objects.filter(tenant_schema=self.tenant.schema_name).delete()
        connection.set_tenant(self.tenant)
        super().tearDown()

    def _logs(self):
        connection.set_schema_to_public()
        rows = list(SystemErrorLog.objects.filter(tenant_schema=self.tenant.schema_name)
                    .values_list('status_code', 'path', 'ip_address'))
        connection.set_tenant(self.tenant)
        return rows

    def test_browser_icon_requests_redirect_and_are_not_logged(self):
        c = HttpClient()
        for p in ('/apple-touch-icon.png', '/apple-touch-icon-precomposed.png',
                  '/apple-touch-icon-120x120.png', '/apple-touch-icon-152x152-precomposed.png',
                  '/favicon.png'):
            r = c.get(p, HTTP_HOST=self.domain.domain)
            self.assertEqual(r.status_code, 301, p)
            self.assertEqual(r['Location'], '/static/icon-192.png')
        self.assertEqual(self._logs(), [])

    def test_bad_forwarded_for_still_records_the_error(self):
        r = HttpClient().get('/api/v1/no-such-endpoint/', HTTP_HOST=self.domain.domain,
                             HTTP_X_FORWARDED_FOR='not-an-ip, 10.0.0.1')
        self.assertEqual(r.status_code, 404)
        logs = self._logs()
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0][1], '/api/v1/no-such-endpoint/')
        self.assertEqual(logs[0][2], '127.0.0.1')   # رجع لـ REMOTE_ADDR

    def test_tenant_360_shows_path_and_separates_visitor_noise(self):
        connection.set_schema_to_public()
        pub = Client.objects.filter(schema_name='public').first()
        if pub is None:
            pub = Client(schema_name='public', name='Platform', owner_name='o', phone='0')
            pub.auto_create_schema = False
            pub.save()
        Domain.objects.get_or_create(domain=PLATFORM_HOST, defaults={'tenant': pub, 'is_primary': False})
        base = dict(tenant_schema=self.tenant.schema_name, method='GET', level='warning')
        for p in ('/api/swagger.json', '/logo.png', '/api/v1/'):
            SystemErrorLog.objects.create(path=p, status_code=404, **base)
        SystemErrorLog.objects.create(path='/.env', status_code=403, **base)
        SystemErrorLog.objects.create(path='/printing/orders/9/detail/', status_code=404,
                                      user_id=7, username='zigzag_owner', **base)
        SystemErrorLog.objects.create(path='/printing/boom/', status_code=500, level='critical',
                                      exception_class='ValueError', message='x',
                                      **{k: v for k, v in base.items() if k != 'level'})
        owner, _ = User.objects.get_or_create(username='noise_sa@test.com',
                                              defaults={'is_staff': True, 'is_superuser': True})
        c = HttpClient()
        c.force_login(owner)
        r = c.get(f'/superadmin/tenant/{self.tenant.pk}/360/', HTTP_HOST=PLATFORM_HOST)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context['unresolved_errors'], 2)        # مستخدم حقيقي + 500
        self.assertEqual(r.context['visitor_noise_errors'], 4)     # 3×404 + 403 من زوار
        html = r.content.decode()
        self.assertIn('/printing/orders/9/detail/', html)
        self.assertIn('zigzag_owner', html)
        self.assertIn('زائر (مش داخل)', html)
        self.assertIn('من زوار/بوتات', html)
        connection.set_tenant(self.tenant)
