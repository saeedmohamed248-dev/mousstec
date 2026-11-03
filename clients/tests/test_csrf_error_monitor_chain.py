"""
🔗 سلسلة واحدة من مركز رصد الأخطاء (11-03): CSRF على السَبدومينات ⇐ المساعد الذكي ⇐ السوبر أدمن ⇐
كرت الصيانة ⇐ مركز الأخطاء نفسه.

* /login/ على دومين شركة (fixit) من موبايل أندرويد: الصفحة مابتتخزّنش (no-store) وبتحط كوكي CSRF،
  والدخول بيعدّي بـ Origin https. ولو التوكن قديم: الفورم بترجع بتوكن جديد والإيميل مكتوب (مش صفحة
  «ارجع وابعت تاني» اللي كانت بترجّع نفس التوكن القديم — اتكرر ٥ مرات في ٦ ثواني).
* نسختين من mt_csrf (مشتركة قديمة + بتاعة الدومين) ⇒ المشتركة بتتمسح، والـ JS بياخد نفس اختيار جانجو.
* /api/ai-assistant/: كان `@csrf_exempt` متحط بالغلط على دالة تانية والـ JS مابيبعتش X-CSRFToken ⇒ 403.
  دلوقتي التوكن بيتبعت + /features/ بتحط الكوكي + حد رسائل للزوار.
* السوبر أدمن: tenant_id مش موجود ⇒ رسالة واضحة ورجوع، مش Http404 متسجّل «500».
* job_card_save 400 ⇒ سبب الرفض بيتسجّل في مركز الأخطاء.
* مركز الأخطاء: بصمة (view + كود + استثناء) وتجميع، Http404 = 404، ومسارات السكانرات بتتقفل لوحدها.
"""
import json
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.models import User
from django.core.cache import cache
from django.db import connection
from django.test import Client as HttpClient, override_settings
from django.utils import timezone

from clients.models import Client, Domain, SystemErrorLog
from clients.services.error_autoheal import autoclose_stale
from clients.services.error_insight import classify, explain, fingerprint
from inventory.tests.base import ERPTenantTestCase

PLATFORM_HOST = 'platform-chain.test.com'
ANDROID = ('Mozilla/5.0 (Linux; Android 14; SM-A546E) AppleWebKit/537.36 (KHTML, like Gecko) '
           'Chrome/130.0.0.0 Mobile Safari/537.36')


def _public_tenant():
    pub = Client.objects.filter(schema_name='public').first()
    if pub is None:
        pub = Client(schema_name='public', name='Platform', owner_name='o', phone='0')
        pub.auto_create_schema = False
        pub.save()
    Domain.objects.get_or_create(domain=PLATFORM_HOST, defaults={'tenant': pub, 'is_primary': False})
    return pub


class _Base(ERPTenantTestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        connection.set_schema_to_public()
        SystemErrorLog.objects.all().delete()
        connection.set_tenant(self.tenant)
        self.host = self.domain.domain

    def tearDown(self):
        connection.set_schema_to_public()
        SystemErrorLog.objects.all().delete()
        connection.set_tenant(self.tenant)
        cache.clear()
        super().tearDown()

    def _logs(self, **flt):
        connection.set_schema_to_public()
        rows = list(SystemErrorLog.objects.filter(**flt).order_by('id'))
        connection.set_tenant(self.tenant)
        return rows


# ─────────────────────────────────────────────────────────────────────
# 1) CSRF على السَبدومينات — دخول شركة fixit من موبايل
# ─────────────────────────────────────────────────────────────────────
@override_settings(ALLOWED_HOSTS=['*'])
class TenantMobileLoginCsrfTests(_Base):
    def setUp(self):
        super().setUp()
        self.owner = User.objects.create_user('fixit_owner', email='owner@fixit.test',
                                              password='S3cure-pass!', is_staff=True, is_superuser=True)
        type(self.tenant).objects.filter(pk=self.tenant.pk).update(is_active=True)

    def _mobile(self):
        return HttpClient(enforce_csrf_checks=True, HTTP_USER_AGENT=ANDROID)

    def _post(self, c, data, **extra):
        return c.post('/login/', data, HTTP_HOST=self.host, secure=True,
                      HTTP_ORIGIN=f'https://{self.host}', HTTP_REFERER=f'https://{self.host}/login/', **extra)

    def test_settings_keep_cookies_host_only_and_trust_every_subdomain(self):
        self.assertIsNone(settings.CSRF_COOKIE_DOMAIN)
        self.assertIsNone(settings.SESSION_COOKIE_DOMAIN)
        self.assertIn(f'https://*.{settings.BASE_DOMAIN}', settings.CSRF_TRUSTED_ORIGINS)

    def test_login_page_is_never_cached_and_always_sets_the_csrf_cookie(self):
        r = self._mobile().get('/login/', HTTP_HOST=self.host, secure=True)
        self.assertEqual(r.status_code, 200)
        self.assertIn('no-store', r['Cache-Control'])
        self.assertIn(settings.CSRF_COOKIE_NAME, r.cookies)
        self.assertFalse(r.cookies[settings.CSRF_COOKIE_NAME]['domain'])     # host-only

    def test_fixit_owner_logs_in_from_android_over_https(self):
        c = self._mobile()
        c.get('/login/', HTTP_HOST=self.host, secure=True)
        tok = c.cookies[settings.CSRF_COOKIE_NAME].value
        r = self._post(c, {'email': 'owner@fixit.test', 'password': 'S3cure-pass!',
                           'csrfmiddlewaretoken': tok})
        self.assertEqual(r.status_code, 302, r.content[:400])
        self.assertIn('/auto-login/', r['Location'])
        self.assertEqual(self._logs(status_code=403), [])

    def test_stale_token_gets_the_login_form_back_with_a_fresh_token(self):
        c = self._mobile()
        c.get('/login/', HTTP_HOST=self.host, secure=True)
        r = self._post(c, {'email': 'owner@fixit.test', 'password': 'S3cure-pass!',
                           'csrfmiddlewaretoken': 'x' * 64})
        self.assertEqual(r.status_code, 403)
        html = r.content.decode()
        self.assertIn('name="csrfmiddlewaretoken"', html)       # فورم الدخول نفسها
        self.assertIn('owner@fixit.test', html)                 # الإيميل مكتوب
        self.assertIn('no-store', r['Cache-Control'])
        log = self._logs(status_code=403)
        self.assertEqual(len(log), 1)
        self.assertIn('CSRF', log[0].message)                    # السبب الحقيقي متسجّل
        # الإرسال التاني من نفس الصفحة يعدّي
        fresh = html.split('name="csrfmiddlewaretoken" value="', 1)[1].split('"', 1)[0]
        r = self._post(c, {'email': 'owner@fixit.test', 'password': 'S3cure-pass!',
                           'csrfmiddlewaretoken': fresh})
        self.assertEqual(r.status_code, 302, r.content[:400])

    def test_missing_cookie_tells_the_user_about_cookies(self):
        r = self._post(self._mobile(), {'email': 'owner@fixit.test', 'password': 'x',
                                        'csrfmiddlewaretoken': 'x' * 64})
        self.assertEqual(r.status_code, 403)
        self.assertIn('الكوكيز', r.content.decode())

    def test_duplicate_shared_csrf_cookie_is_deleted(self):
        c = self._mobile()
        c.get('/login/', HTTP_HOST=self.host, secure=True)
        tok = c.cookies[settings.CSRF_COOKIE_NAME].value
        r = c.get('/login/', HTTP_HOST=self.host, secure=True,
                  HTTP_COOKIE=f'mt_csrf=OLDSHARED; mt_csrf={tok}')
        cookie = r.cookies.get(settings.CSRF_COOKIE_NAME)
        self.assertIsNotNone(cookie)
        self.assertEqual(cookie['domain'], f'.{settings.BASE_DOMAIN}')
        self.assertEqual(cookie['max-age'], 0)

    def test_js_picks_the_same_csrf_cookie_as_django(self):
        js = open('static/js/pwa-init.js', encoding='utf-8').read()
        self.assertIn('window.mtCsrfToken = currentToken', js)
        self.assertIn("v8.3.0", open('static/sw.js', encoding='utf-8').read())   # الكاش يتجدد


# ─────────────────────────────────────────────────────────────────────
# 2) المساعد الذكي على /features/
# ─────────────────────────────────────────────────────────────────────
@override_settings(ALLOWED_HOSTS=['*'], ENABLE_AI_PREDICTIONS=False)
class AiAssistantCsrfTests(_Base):
    def setUp(self):
        super().setUp()
        connection.set_schema_to_public()
        _public_tenant()

    def _ask(self, c, msg='الأسعار كام؟', token=None, ip='203.0.113.9'):
        headers = {'HTTP_HOST': PLATFORM_HOST, 'HTTP_X_FORWARDED_FOR': ip}
        if token is not None:
            headers['HTTP_X_CSRFTOKEN'] = token
        return c.post('/api/ai-assistant/', data=json.dumps({'message': msg, 'history': []}),
                      content_type='application/json', **headers)

    def test_features_page_sets_cookie_and_the_chat_works_with_the_header(self):
        c = HttpClient(enforce_csrf_checks=True)
        r = c.get('/features/', HTTP_HOST=PLATFORM_HOST)
        self.assertEqual(r.status_code, 200)
        self.assertIn(settings.CSRF_COOKIE_NAME, r.cookies)
        r = self._ask(c, token=c.cookies[settings.CSRF_COOKIE_NAME].value)
        self.assertEqual(r.status_code, 200, r.content[:300])
        self.assertEqual(r.json()['status'], 'ok')

    def test_without_the_header_it_is_rejected_as_csrf_json(self):
        c = HttpClient(enforce_csrf_checks=True)
        c.get('/features/', HTTP_HOST=PLATFORM_HOST)
        r = self._ask(c)
        self.assertEqual(r.status_code, 403)
        self.assertTrue(r.json()['retry'])

    def test_widget_sends_the_csrf_header(self):
        html = open('clients/templates/clients/ai_chatbot_widget.html', encoding='utf-8').read()
        chat = html.split("fetch('/api/ai-assistant/'", 1)[1][:400]
        self.assertIn("'X-CSRFToken': aiCsrfToken()", chat)

    def test_visitors_are_rate_limited_and_members_have_more_room(self):
        from clients.views.ai_assistant_views import VISITOR_LIMITS
        c = HttpClient()
        per_min = VISITOR_LIMITS[0][0]
        codes = [self._ask(c, ip='198.51.100.7').status_code for _ in range(per_min + 1)]
        self.assertEqual(codes[:per_min], [200] * per_min)
        self.assertEqual(codes[-1], 429)
        # IP تاني مايتأثرش
        self.assertEqual(self._ask(c, ip='198.51.100.8').status_code, 200)

    def test_garbage_body_is_a_clean_400(self):
        r = HttpClient().post('/api/ai-assistant/', data='[1,2]', content_type='application/json',
                              HTTP_HOST=PLATFORM_HOST)
        self.assertEqual(r.status_code, 400)


# ─────────────────────────────────────────────────────────────────────
# 3) السوبر أدمن: tenant_id مش موجود
# ─────────────────────────────────────────────────────────────────────
@override_settings(ALLOWED_HOSTS=['*'])
class SuperAdminMissingTenantTests(_Base):
    def test_action_on_deleted_tenant_redirects_with_message_not_500(self):
        connection.set_schema_to_public()
        _public_tenant()
        owner = User.objects.create_user('chain_sa@test.com', password='x', is_staff=True, is_superuser=True)
        c = HttpClient()
        c.force_login(owner)
        for bad in ('987654321', 'abc'):
            r = c.post('/superadmin/', {'action': 'suspend', 'tenant_id': bad}, HTTP_HOST=PLATFORM_HOST)
            self.assertEqual(r.status_code, 302, bad)
            self.assertIn('/superadmin/', r['Location'])
        msgs = [str(m) for m in r.wsgi_request._messages]
        self.assertTrue(any('مش موجودة' in m for m in msgs), msgs)
        self.assertEqual([e for e in self._logs() if e.status_code >= 500], [])


# ─────────────────────────────────────────────────────────────────────
# 4) كرت الصيانة: سبب الـ 400 بيتسجّل
# ─────────────────────────────────────────────────────────────────────
@override_settings(ALLOWED_HOSTS=['*'])
class JobCardRejectionLoggedTests(_Base):
    def test_400_reason_is_recorded_in_the_error_log(self):
        from inventory.tests.factories import make_branch
        make_branch()
        owner = User.objects.create_user('jc_owner', password='x', is_staff=True, is_superuser=True)
        c = HttpClient()
        c.force_login(owner)
        r = c.post('/system/job-card/save/', data=json.dumps({'customer_name': 'x', 'items': [], 'services': []}),
                   content_type='application/json', HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 400)
        log = self._logs(status_code=400)
        self.assertEqual(len(log), 1)
        self.assertIn(r.json()['error'], log[0].message)
        self.assertEqual(log[0].fingerprint, fingerprint(400, '/system/job-card/save/'))


# ─────────────────────────────────────────────────────────────────────
# 5) مركز الأخطاء: بصمة + تجميع + Http404 + السكانرات
# ─────────────────────────────────────────────────────────────────────
@override_settings(ALLOWED_HOSTS=['*'])
class ErrorMonitorFingerprintTests(_Base):
    def test_http404_from_a_view_is_recorded_as_404_not_500(self):
        connection.set_schema_to_public()
        _public_tenant()
        owner = User.objects.create_user('nf_owner@test.com', password='x', is_staff=True, is_superuser=True)
        c = HttpClient()
        c.force_login(owner)
        r = c.get('/superadmin/tenant/987654321/grants/', HTTP_HOST=PLATFORM_HOST)   # get_object_or_404
        self.assertEqual(r.status_code, 404)
        logs = self._logs(path='/superadmin/tenant/987654321/grants/')
        self.assertEqual(len(logs), 1)
        e = logs[0]
        self.assertEqual((e.status_code, e.level, e.exception_class, e.traceback), (404, 'warning', 'Http404', ''))
        self.assertNotEqual(explain(e)['kind'], 'code')

    def test_legacy_http404_rows_logged_as_500_are_not_code_errors(self):
        self.assertNotEqual(classify(500, '/superadmin/', 1, 'Http404'), 'code')
        self.assertEqual(classify(500, '/superadmin/', 1, 'ValueError'), 'code')

    def test_same_view_different_ids_share_a_fingerprint(self):
        self.assertEqual(fingerprint(404, '/system/orders/12/x/'), fingerprint(404, '/system/orders/99/x/'))
        self.assertNotEqual(fingerprint(404, '/system/orders/12/x/'), fingerprint(403, '/system/orders/12/x/'))
        self.assertNotEqual(fingerprint(500, '/login/', 'ValueError'), fingerprint(500, '/login/', 'KeyError'))
        self.assertEqual(fingerprint(500, '/superadmin/', 'Http404'), fingerprint(404, '/superadmin/', 'Http404'))

    def test_scanner_paths_auto_close_anywhere_in_the_path_even_when_logged_in(self):
        for p in ('/api/actuator/env', '/blog/wp-login.php', '/x/.env', '/.git/config'):
            self.assertEqual(classify(404, p, user_id=5, ua=ANDROID), 'bot', p)
        self.assertNotEqual(classify(404, '/system/environment/', user_id=5, ua=ANDROID), 'bot')
        r = HttpClient().get('/api/actuator/env', HTTP_HOST=self.host, HTTP_USER_AGENT=ANDROID,
                             HTTP_REFERER=f'https://{self.host}/')
        self.assertIn(r.status_code, (403, 404))
        logs = self._logs(path='/api/actuator/env')
        self.assertTrue(logs and all(e.is_resolved for e in logs))

    def test_autoclose_closes_old_open_scanner_rows(self):
        connection.set_schema_to_public()
        old = SystemErrorLog.objects.create(tenant_schema='public', path='/api/actuator/env', method='GET',
                                            status_code=404, level='warning')
        real = SystemErrorLog.objects.create(tenant_schema='public', path='/system/orders/', method='GET',
                                             status_code=404, level='warning', user_id=3)
        autoclose_stale()
        old.refresh_from_db()
        real.refresh_from_db()
        self.assertTrue(old.is_resolved)
        self.assertFalse(real.is_resolved)
        connection.set_tenant(self.tenant)

    def test_dashboard_groups_by_fingerprint_and_resolve_closes_the_group(self):
        connection.set_schema_to_public()
        _public_tenant()
        schema = self.tenant.schema_name
        fp = fingerprint(403, '/login/')
        now = timezone.now()
        for i in range(5):
            e = SystemErrorLog.objects.create(tenant_schema=schema, path='/login/', method='POST', status_code=403,
                                              level='error', fingerprint=fp, user_id=None,
                                              request_data={'UA': ANDROID, 'REF': f'https://{self.host}/login/'})
            SystemErrorLog.objects.filter(pk=e.pk).update(created_at=now - timedelta(seconds=10 - i))
        other = SystemErrorLog.objects.create(tenant_schema=schema, path='/system/x/', method='GET',
                                              status_code=500, level='critical', exception_class='ValueError',
                                              fingerprint=fingerprint(500, '/system/x/', 'ValueError'))
        sa = User.objects.create_user('chain_errs@test.com', password='x', is_staff=True, is_superuser=True)
        c = HttpClient()
        c.force_login(sa)
        r = c.get('/superadmin/errors/?show=open', HTTP_HOST=PLATFORM_HOST)
        self.assertEqual(r.status_code, 200)
        rows = r.context['errors']
        self.assertEqual(len(rows), 2)
        login_row = next(e for e in rows if e.path == '/login/')
        self.assertEqual(login_row.repeat_count, 5)
        self.assertEqual(login_row.insight['kind'], 'csrf')
        r = c.post(f'/superadmin/errors/{login_row.pk}/resolve/', HTTP_HOST=PLATFORM_HOST)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(SystemErrorLog.objects.filter(fingerprint=fp, is_resolved=False).count(), 0)
        other.refresh_from_db()
        self.assertFalse(other.is_resolved)
        self.assertEqual(explain(other)['kind'], 'code')
        connection.set_tenant(self.tenant)
