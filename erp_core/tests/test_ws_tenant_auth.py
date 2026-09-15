"""مصادقة الويب سوكيت لازم تشتغل في schema الفرع — ومتمسّش جلسة حد أبداً.

السبب الجذري لعطل "Bad Request (400)" في الإنتاج:

مسار الويب سوكيت في ASGI مبيعدّيش على TenantMainMiddleware، فالـ schema
النشط وقت تنفيذه بيفضل `public`. و`auth_user` في TENANT_APPS، يعني
`id=1` في public ده مالك المنصة مش صاحب الجلسة. النتيجة إن
`channels.auth.get_user` بيلاقي مستخدم غلط → الـ session auth hash ما
بيطابقش → بيعمل `session.flush()` → **صفّ الجلسة يتمسح من الداتابيز**.

وبعدها المستخدم على الموقع العادي: الصفحات بتفتح (الجلسة لسه في كاش Redis)
لكن أول طلب بيكتب في الجلسة بيرمي SessionInterrupted → صفحة 400.

الاختبارات دي بتثبّت السلوكين الصح: البحث في schema الفرع، وعدم المسح نهائياً.
"""
from asgiref.sync import async_to_sync
from django.conf import settings
from django.contrib.auth import (
    BACKEND_SESSION_KEY, HASH_SESSION_KEY, SESSION_KEY,
)
from django.contrib.auth.models import AnonymousUser, User

from erp_core.asgi import _resolve_ws_user
from inventory.tests.base import ERPTenantTestCase

_BACKEND = 'clients.backends.CaseInsensitiveEmailBackend'


class _SpySession(dict):
    """جلسة بتسجّل لو حد نده flush عليها."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.flushed = False

    def flush(self):
        self.flushed = True
        self.clear()


class WebSocketTenantAuthTests(ERPTenantTestCase):

    def setUp(self):
        self.user = User.objects.create_user(
            username='ws_user', email='ws@test.local', password='pw-12345')

    def _scope(self, schema_name=None, session_hash=None, user_id=None):
        session = _SpySession({
            SESSION_KEY: str(user_id if user_id is not None else self.user.pk),
            BACKEND_SESSION_KEY: _BACKEND,
            HASH_SESSION_KEY: (session_hash if session_hash is not None
                               else self.user.get_session_auth_hash()),
        })
        return {'session': session,
                'schema_name': schema_name or self.tenant.schema_name}

    def test_backend_path_is_configured(self):
        """لو الباك-إند اتشال من الإعدادات، باقي الاختبارات تبقى بلا معنى."""
        self.assertIn(_BACKEND, settings.AUTHENTICATION_BACKENDS)

    def test_user_is_resolved_inside_the_tenant_schema(self):
        scope = self._scope()
        user = async_to_sync(_resolve_ws_user)(scope)
        self.assertEqual(user.pk, self.user.pk)
        self.assertFalse(scope['session'].flushed)

    def test_hash_mismatch_returns_anonymous_without_flushing(self):
        """ده اللي كان بيمسح الجلسة ويكسر الموقع — دلوقتي زائر مجهول وبس."""
        scope = self._scope(session_hash='not-the-right-hash')
        user = async_to_sync(_resolve_ws_user)(scope)
        self.assertIsInstance(user, AnonymousUser)
        self.assertFalse(scope['session'].flushed, 'الجلسة اتمسحت — العطل رجع')

    def test_unknown_user_returns_anonymous_without_flushing(self):
        scope = self._scope(user_id=99999999)
        user = async_to_sync(_resolve_ws_user)(scope)
        self.assertIsInstance(user, AnonymousUser)
        self.assertFalse(scope['session'].flushed)

    def test_anonymous_scope_is_handled(self):
        for scope in ({}, {'session': _SpySession()},
                      {'session': _SpySession({SESSION_KEY: '1'})}):
            user = async_to_sync(_resolve_ws_user)(scope)
            self.assertIsInstance(user, AnonymousUser)

    def test_untrusted_backend_path_is_rejected(self):
        scope = self._scope()
        scope['session'][BACKEND_SESSION_KEY] = 'evil.backends.Backdoor'
        user = async_to_sync(_resolve_ws_user)(scope)
        self.assertIsInstance(user, AnonymousUser)
