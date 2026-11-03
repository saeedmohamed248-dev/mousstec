"""
🩺 المستوى ٢ (علاج وتنبيه تلقائي) + 🤖 المستوى ٣ (بوت التصليح).

* فشل CSRF ⇒ صفحة عربي «ارجع وابعت تاني» + كوكي جديد (كانت صفحة جانجو الإنجليزي).
* بصمة الخطأ: نفس العطل في نفس الدالة = نفس البصمة حتى لو الأرقام في الرابط مختلفة.
* عطل 500 جديد / بيتكرر كتير / عند ٣ شركات ⇒ إيميل للمالك مرة واحدة (مش سيل).
* خطأ مفتوح ماتكررش ١٤ يوم ⇒ بيتقفل لوحده.
* بوت التصليح: بيبعت لـ GitHub من غير أسماء/إيميلات/موبايلات، مرة واحدة لكل بصمة، وبحد يومي.
"""
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.core import mail
from django.core.cache import cache
from django.db import connection
from django.test import Client as HttpClient, override_settings
from django.utils import timezone

from clients import tasks
from clients.models import Client, Domain, SystemErrorLog
from clients.services import autofix
from clients.services.error_autoheal import (
    alert_text, autoclose_stale, normalize_path, on_error_recorded, signature, signature_of,
)
from .base import ERPTenantTestCase

PLATFORM_HOST = 'platform-heal.test.com'
_IPHONE = 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) Version/17.0 Mobile Safari/604.1'
_TB = '''Traceback (most recent call last):
  File "/usr/local/lib/python3.12/site-packages/django/core/handlers/base.py", line 197, in _get_response
    response = wrapped_callback(request)
  File "/app/printing/views.py", line 412, in order_create
    total = qty * price
ValueError: invalid literal for int() with base 10: 'ahmed@zigzag.com 01002289984'
'''


def _err(schema, **kw):
    data = dict(tenant_schema=schema, tenant_name='Zigzag Advertising', path='/printing/orders/12/create/',
                method='POST', status_code=500, exception_class='ValueError',
                message="invalid literal: 'ahmed@zigzag.com' phone 01002289984", traceback=_TB,
                level='critical', user_id=4, username='zigzag_owner',
                request_data={'UA': _IPHONE, 'REF': '', 'GET': {}})
    data.update(kw)
    with_schema = connection.schema_name
    connection.set_schema_to_public()
    e = SystemErrorLog.objects.create(**data)
    if with_schema != 'public':
        connection.set_tenant(Client.all_objects.get(schema_name=with_schema))
    return e


class _Base(ERPTenantTestCase):
    def setUp(self):
        cache.clear()
        connection.set_schema_to_public()
        SystemErrorLog.objects.all().delete()
        connection.set_tenant(self.tenant)

    def tearDown(self):
        connection.set_schema_to_public()
        SystemErrorLog.objects.all().delete()
        connection.set_tenant(self.tenant)
        cache.clear()
        super().tearDown()


@override_settings(ALLOWED_HOSTS=['*'])
class CsrfSelfHealTests(_Base):
    def test_csrf_failure_page_is_arabic_and_reissues_the_cookie(self):
        c = HttpClient(enforce_csrf_checks=True)
        # (صفحة /login/ نفسها بترجع فورم الدخول بتوكن جديد — clients/tests/test_csrf_error_monitor_chain.py)
        r = c.post('/account/verify-email/resend/', {'email': 'x'}, HTTP_HOST=self.domain.domain,
                   HTTP_REFERER=f'http://{self.domain.domain}/signup/?next=/x/')
        self.assertEqual(r.status_code, 403)
        html = r.content.decode()
        self.assertIn('ارجع وابعت تاني', html)
        self.assertIn('data-back="/signup/?next=/x/"', html)
        self.assertNotIn('CSRF verification failed', html)
        self.assertIn('mt_csrf', r.cookies)                       # كوكي جديد ⇒ الإرسال الجاي يعدّي

    def test_csrf_failure_for_fetch_returns_json(self):
        r = HttpClient(enforce_csrf_checks=True).post(
            '/login/', {}, HTTP_HOST=self.domain.domain, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()['error'], 'csrf_failed')
        self.assertTrue(r.json()['retry'])

    def test_external_referer_is_not_used_as_back_link(self):
        r = HttpClient(enforce_csrf_checks=True).post(
            '/account/verify-email/resend/', {}, HTTP_HOST=self.domain.domain,
            HTTP_REFERER='https://evil.example/phish')
        self.assertIn('data-back="/"', r.content.decode())

    def test_form_token_is_refreshed_from_cookie_on_submit(self):
        js = open('static/js/pwa-init.js', encoding='utf-8').read()
        self.assertIn("'mt_csrf'", js)
        self.assertIn("csrfmiddlewaretoken", js)
        self.assertIn("v8.3.0", open('static/sw.js', encoding='utf-8').read())   # الكاش يتجدد


class SignatureTests(_Base):
    def test_same_bug_different_ids_same_signature(self):
        self.assertEqual(normalize_path('/orders/12/edit/'), '/orders/{id}/edit/')
        self.assertEqual(signature(404, '/orders/12/x/'), signature(404, '/orders/99/x/'))
        self.assertNotEqual(signature(404, '/orders/12/x/'), signature(404, '/clients/12/x/'))
        # 500: الدالة اللي وقع فيها هي اللي بتفرّق، مش الرابط
        self.assertEqual(signature(500, '/a/1/', 'ValueError', _TB), signature(500, '/b/2/', 'ValueError', _TB))


class AlertRulesTests(_Base):
    def setUp(self):
        super().setUp()
        self.delay = mock.patch.object(tasks.send_error_alert, 'delay').start()
        self.fix = mock.patch.object(tasks.request_error_autofix, 'delay').start()
        self.addCleanup(mock.patch.stopall)

    def test_new_crash_alerts_once(self):
        e1 = _err(self.tenant.schema_name)
        self.assertEqual(on_error_recorded(e1), ['new'])
        self.delay.assert_called_once_with(e1.pk, ['new'])
        e2 = _err(self.tenant.schema_name, path='/printing/orders/77/create/')
        self.assertEqual(on_error_recorded(e2), [])              # نفس البصمة — مش جديد
        self.assertEqual(self.delay.call_count, 1)

    def test_autofix_is_requested_only_when_enabled(self):
        on_error_recorded(_err(self.tenant.schema_name))
        self.fix.assert_not_called()
        cache.clear()
        with override_settings(AUTOFIX_GITHUB_TOKEN='t'):
            e = _err(self.tenant.schema_name)
            on_error_recorded(e)
        self.fix.assert_called_once_with(e.pk)

    def test_spike_alerts_once_per_window(self):
        reasons = [on_error_recorded(_err(self.tenant.schema_name, status_code=403, exception_class='',
                                          traceback='', path='/system/treasury/', method='GET'))
                   for _ in range(25)]
        flat = [r for rs in reasons for r in rs]
        self.assertEqual(flat, ['spike'])                       # بعد ٢٠ مرة — ومرة واحدة بس

    def test_same_error_at_three_companies(self):
        out = []
        for schema in ('shop_a', 'shop_b', 'shop_c', 'shop_d'):
            out += on_error_recorded(_err(schema, status_code=404, exception_class='', traceback='',
                                          method='GET', path='/system/reports/x/',
                                          request_data={'UA': _IPHONE, 'REF': 'https://shop.mousstec.com/system/'}))
        self.assertEqual(out, ['tenants'])

    def test_bots_never_alert(self):
        e = _err(self.tenant.schema_name, status_code=404, exception_class='', traceback='', user_id=None,
                 username='', path='/.env', method='GET', request_data={'UA': 'zgrab/0.x'})
        self.assertEqual(on_error_recorded(e), [])
        self.delay.assert_not_called()


@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
                   SUPPORT_INBOX_EMAIL='owner@plat.test')
class AlertEmailTests(_Base):
    def test_alert_email_says_where_exactly(self):
        e = _err(self.tenant.schema_name)
        tasks.send_error_alert(e.pk, ['new'])
        self.assertEqual(len(mail.outbox), 1)
        m = mail.outbox[0]
        self.assertEqual(m.to, ['owner@plat.test'])
        self.assertIn('عطل جديد', m.subject)
        self.assertIn('printing/views.py:412 — order_create()', m.body)
        self.assertIn('Zigzag Advertising', m.body)
        subject, body = alert_text(e, ['spike', 'tenants'])
        self.assertIn('بيتكرر كتير', body)
        self.assertIn('3 شركات', body)


class AutoCloseTests(_Base):
    def test_errors_not_seen_for_14_days_close_themselves(self):
        old = _err(self.tenant.schema_name, path='/old/', exception_class='', status_code=404, traceback='')
        still = _err(self.tenant.schema_name, path='/again/', exception_class='', status_code=404, traceback='')
        connection.set_schema_to_public()
        SystemErrorLog.objects.filter(pk__in=[old.pk, still.pk]).update(
            created_at=timezone.now() - timedelta(days=20))
        connection.set_tenant(self.tenant)
        _err(self.tenant.schema_name, path='/again/', exception_class='', status_code=404, traceback='')
        connection.set_schema_to_public()
        self.assertEqual(autoclose_stale(), 1)
        old.refresh_from_db(); still.refresh_from_db()
        self.assertTrue(old.is_resolved)
        self.assertIsNone(old.resolved_by_id)
        self.assertFalse(still.is_resolved)                     # اتكرر من قريب — يفضل مفتوح
        connection.set_tenant(self.tenant)


class _Resp:
    def __init__(self, code):
        self.status_code, self.text = code, ''


@override_settings(AUTOFIX_GITHUB_TOKEN='ghp_test', AUTOFIX_GITHUB_REPO='owner/repo', AUTOFIX_DAILY_LIMIT=2)
class AutofixDispatchTests(_Base):
    def test_disabled_without_token(self):
        with override_settings(AUTOFIX_GITHUB_TOKEN=''):
            ok, msg = autofix.dispatch(_err(self.tenant.schema_name))
        self.assertFalse(ok)
        self.assertIn('AUTOFIX_GITHUB_TOKEN', msg)

    def test_payload_has_where_but_no_personal_data(self):
        e = _err(self.tenant.schema_name)
        with mock.patch('requests.post', return_value=_Resp(204)) as post:
            ok, _ = autofix.dispatch(e)
        self.assertTrue(ok)
        url = post.call_args.args[0]
        body = post.call_args.kwargs['json']
        self.assertEqual(url, 'https://api.github.com/repos/owner/repo/dispatches')
        self.assertEqual(body['event_type'], 'autofix-error')
        p = body['client_payload']['error']
        self.assertEqual(p['signature'], signature_of(e))
        self.assertEqual(p['path'], '/printing/orders/{id}/create/')
        self.assertEqual(p['code_at']['file'], 'printing/views.py')
        dumped = str(p)
        for secret in ('ahmed@zigzag.com', '01002289984', 'Zigzag', 'zigzag_owner'):
            self.assertNotIn(secret, dumped)
        self.assertEqual(post.call_args.kwargs['headers']['Authorization'], 'Bearer ghp_test')

    def test_once_per_signature_and_daily_limit(self):
        with mock.patch('requests.post', return_value=_Resp(204)) as post:
            self.assertTrue(autofix.dispatch(_err(self.tenant.schema_name))[0])
            self.assertFalse(autofix.dispatch(_err(self.tenant.schema_name))[0])          # نفس البصمة
            self.assertTrue(autofix.dispatch(_err(self.tenant.schema_name), force=True)[0])  # يدوي
            ok, msg = autofix.dispatch(_err(self.tenant.schema_name), force=True)
        self.assertFalse(ok)                                                             # الحد اليومي = 2
        self.assertIn('الحد اليومي', msg)
        self.assertEqual(post.call_count, 2)

    def test_github_rejection_is_reported(self):
        with mock.patch('requests.post', return_value=_Resp(404)):
            ok, msg = autofix.dispatch(_err(self.tenant.schema_name))
        self.assertFalse(ok)
        self.assertIn('404', msg)
        self.assertIsNone(autofix.sent_at(signature_of(_err(self.tenant.schema_name))))   # يتبعت تاني


@override_settings(ALLOWED_HOSTS=['*'], AUTOFIX_GITHUB_TOKEN='ghp_test', AUTOFIX_GITHUB_REPO='owner/repo')
class AutofixButtonTests(_Base):
    def test_errors_center_button_sends_to_bot(self):
        connection.set_schema_to_public()
        pub = Client.objects.filter(schema_name='public').first()
        if pub is None:
            pub = Client(schema_name='public', name='Platform', owner_name='o', phone='0')
            pub.auto_create_schema = False
            pub.save()
        Domain.objects.get_or_create(domain=PLATFORM_HOST, defaults={'tenant': pub, 'is_primary': False})
        e = _err('public')
        owner, _ = User.objects.get_or_create(username='heal_sa@test.com',
                                              defaults={'is_staff': True, 'is_superuser': True})
        c = HttpClient()
        c.force_login(owner)
        html = c.get('/superadmin/errors/', HTTP_HOST=PLATFORM_HOST).content.decode()
        self.assertIn(f'/superadmin/errors/{e.pk}/autofix/', html)
        with mock.patch('requests.post', return_value=_Resp(204)):
            r = c.post(f'/superadmin/errors/{e.pk}/autofix/', {'next': '/superadmin/errors/?show=all'},
                       HTTP_HOST=PLATFORM_HOST, follow=True)
        page = r.content.decode()
        self.assertIn('اتبعت لبوت التصليح', page)
        self.assertNotIn(f'/superadmin/errors/{e.pk}/autofix/', page)    # مكانه بقى «اتبعت»
        connection.set_tenant(self.tenant)
