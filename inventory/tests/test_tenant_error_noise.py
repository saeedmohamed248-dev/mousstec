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


# ─────────────────────────────────────────────────────────────────────
# 🔎 «المشكلة فين بالظبط وفي أنهي صفحة» + البوتات بتتقفل لوحدها + الـ honeypot
# ─────────────────────────────────────────────────────────────────────
from django.core.cache import cache  # noqa: E402
from django.urls import path as _path  # noqa: E402

import erp_core.urls as _erp_urls  # noqa: E402
from clients.services.error_insight import classify, code_location, explain, page_title  # noqa: E402


def _boom(request):
    raise ValueError('حساب الإجمالي باظ')


urlpatterns = list(_erp_urls.urlpatterns) + [_path('zz-boom/', _boom)]
handler404 = _erp_urls.handler404
handler500 = _erp_urls.handler500

_TB = '''Traceback (most recent call last):
  File "/usr/local/lib/python3.12/site-packages/django/core/handlers/base.py", line 197, in _get_response
    response = wrapped_callback(request, *callback_args, **callback_kwargs)
  File "/app/printing/views.py", line 412, in order_create
    total = qty * price
  File "/usr/local/lib/python3.12/site-packages/django/db/models/query.py", line 10, in get
    raise self.model.DoesNotExist
ValueError: boom
'''
_IPHONE = ('Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 '
           '(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1')


class ErrorInsightUnitTests(ERPTenantTestCase):
    def test_classify(self):
        ref = f'https://{self.domain.domain}/secure-portal/printing/printorder/'
        self.assertEqual(classify(404, '/.env'), 'bot')
        self.assertEqual(classify(404, '/x/', ua='WhatsApp/2.23'), 'bot')
        self.assertEqual(classify(404, '/x/', ua=_IPHONE), 'bot')            # زائر من برّه
        self.assertEqual(classify(404, '/x/', user_id=3, ua=_IPHONE, referer=ref), 'not_found')   # دومين مش معروف
        self.assertEqual(classify(404, '/x/', user_id=3, ua=_IPHONE, referer=ref, hosts=[self.domain.domain]),
                         'broken_link')
        self.assertEqual(classify(403, '/system/x/', user_id=3, ua=_IPHONE), 'permission')
        self.assertEqual(classify(403, '/login/', ua=_IPHONE, referer=ref, method='POST', hosts=[self.domain.domain]), 'csrf')
        self.assertEqual(classify(500, '/x/', exception_class='ValueError'), 'code')
        self.assertEqual(classify(400, '/x/', user_id=3, ua=_IPHONE), 'bad_request')

    def test_page_and_code_location(self):
        self.assertIn('#12', str(page_title('/secure-portal/printing/printorder/12/change/')))
        self.assertEqual(str(page_title('/no/such/page/')), 'مسار مش موجود في السيستم')
        loc = code_location(_TB)
        self.assertEqual((loc['file'], loc['line'], loc['func']), ('printing/views.py', 412, 'order_create'))
        self.assertEqual(loc['src'], 'total = qty * price')

    def test_explain_says_which_page_the_user_was_on(self):
        info = explain({'status_code': 404, 'path': '/printing/orders/99/detail/', 'method': 'GET',
                        'user_id': 4, 'username': 'zigzag_owner',
                        'request_data': {'UA': _IPHONE,
                                         'REF': f'https://{self.domain.domain}/secure-portal/printing/printorder/7/change/'}},
                       hosts=[self.domain.domain])
        self.assertEqual(info['kind'], 'broken_link')
        self.assertEqual(info['from_page']['path'], '/secure-portal/printing/printorder/7/change/')
        self.assertIn('#7', str(info['from_page']['title']))
        self.assertIn('آيفون', str(info['device']))
        self.assertEqual(info['who'], 'zigzag_owner')


@override_settings(ALLOWED_HOSTS=['*'], ROOT_URLCONF=__name__)
class ErrorRecordingTests(ERPTenantTestCase):
    def setUp(self):
        connection.set_schema_to_public()
        SystemErrorLog.objects.filter(tenant_schema=self.tenant.schema_name).delete()
        connection.set_tenant(self.tenant)

    def _rows(self):
        connection.set_schema_to_public()
        rows = list(SystemErrorLog.objects.filter(tenant_schema=self.tenant.schema_name).order_by('id'))
        connection.set_tenant(self.tenant)
        return rows

    def test_bot_noise_is_auto_closed_but_real_errors_stay_open(self):
        c = HttpClient()
        c.get('/wp-login.php', HTTP_HOST=self.domain.domain, HTTP_USER_AGENT=_IPHONE)
        u = User.objects.create_user('err_owner', password='x', is_superuser=True, is_staff=True)
        c.force_login(u)
        c.get('/api/v1/gone/', HTTP_HOST=self.domain.domain, HTTP_USER_AGENT=_IPHONE,
              HTTP_REFERER=f'http://{self.domain.domain}/secure-portal/')
        rows = self._rows()
        self.assertEqual([(r.path, r.is_resolved) for r in rows],
                         [('/wp-login.php', True), ('/api/v1/gone/', False)])
        self.assertIsNone(rows[0].resolved_by_id)
        self.assertIsNotNone(rows[0].resolved_at)

    def test_crash_is_recorded_once_with_its_traceback(self):
        c = HttpClient(raise_request_exception=False)
        r = c.get('/zz-boom/', HTTP_HOST=self.domain.domain, HTTP_USER_AGENT=_IPHONE)
        self.assertEqual(r.status_code, 500)
        rows = self._rows()
        self.assertEqual(len(rows), 1)                    # كان بيتسجّل مرتين
        self.assertEqual(rows[0].exception_class, 'ValueError')
        loc = code_location(rows[0].traceback)
        self.assertEqual(loc['file'], 'inventory/tests/test_tenant_error_noise.py')
        self.assertEqual(loc['func'], '_boom')

    def test_honeypot_does_not_write_a_fake_ban(self):
        cache.clear()
        r = HttpClient().get('/.env', HTTP_HOST=self.domain.domain, HTTP_X_FORWARDED_FOR='41.33.1.2')
        self.assertEqual(r.status_code, 403)
        self.assertIsNone(cache.get('mousstec_cyber_ban_41.33.1.2'))


@override_settings(ALLOWED_HOSTS=['*'])
class ErrorsCenterPageTests(ERPTenantTestCase):
    def test_errors_center_shows_where_exactly(self):
        connection.set_schema_to_public()
        pub = Client.objects.filter(schema_name='public').first()
        if pub is None:
            pub = Client(schema_name='public', name='Platform', owner_name='o', phone='0')
            pub.auto_create_schema = False
            pub.save()
        Domain.objects.get_or_create(domain=PLATFORM_HOST, defaults={'tenant': pub, 'is_primary': False})
        s = self.tenant.schema_name
        for _ in range(3):
            SystemErrorLog.objects.create(
                tenant_schema=s, path='/printing/orders/99/detail/', method='GET', status_code=404,
                user_id=4, username='zigzag_owner', level='warning',
                request_data={'UA': _IPHONE, 'REF': f'https://{self.domain.domain}/secure-portal/printing/printorder/7/change/'})
        SystemErrorLog.objects.create(tenant_schema=s, path='/zz/', method='POST', status_code=500,
                                      exception_class='ValueError', message='x', traceback=_TB, level='critical')
        owner, _ = User.objects.get_or_create(username='errc_sa@test.com',
                                              defaults={'is_staff': True, 'is_superuser': True})
        c = HttpClient()
        c.force_login(owner)
        r = c.get(f'/superadmin/errors/?show=open&tenant={s}', HTTP_HOST=PLATFORM_HOST)
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        self.assertIn('رابط مكسور', html)
        self.assertIn('/secure-portal/printing/printorder/7/change/', html)    # كان واقف فين
        self.assertIn('اتكرر 3 مرة', html)
        self.assertIn('printing/views.py:412', html)                          # الملف والسطر
        self.assertIn('order_create', html)
        self.assertIn(f'?show=resolved&tenant={s}', html)                  # التابات بتحافظ على الشركة
        SystemErrorLog.objects.filter(tenant_schema=s).delete()
        connection.set_tenant(self.tenant)
