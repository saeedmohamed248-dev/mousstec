"""صفّ جلسة مفقود من الداتابيز ما ينفعش يطلّع للمستخدم صفحة 400.

السيناريو الحقيقي اللي اتصاد في الإنتاج (لوج tenant `fixit`):

    400 Bad Request — path=/system/switch-branch/ method=GET user=1
    exc=SessionInterrupted("The request's session was deleted before the
    request completed...")

الجلسات مخزّنة `cached_db`: القراءة من Redis والكتابة UPDATE على الداتابيز.
لما صفّ الجلسة يكون مفقود من `django_session`:

  • صفحات القراءة تفتح عادي (الجلسة لسه في الكاش) — فالمستخدم مش حاسس بحاجة.
  • أول طلب **يكتب** في الجلسة (تبديل الفرع مثلاً) الـ UPDATE يرجّع صفر صفوف
    → `SessionInterrupted` → صفحة "Bad Request (400)" خام.

`ResilientSessionMiddleware` بينشئ الصف من تاني بنفس بيانات الجلسة ويكمّل.
"""
from importlib import import_module

from django.conf import settings
from django.contrib.sessions.backends.base import UpdateError
from django.contrib.sessions.exceptions import SessionInterrupted
from django.contrib.sessions.middleware import SessionMiddleware
from django.http import HttpResponse
from django.test import RequestFactory, TestCase

from erp_core.middleware import ResilientSessionMiddleware

_SessionStore = import_module(settings.SESSION_ENGINE).SessionStore


class _VanishingRowSession(_SessionStore):
    """جلسة صفّها اتفقد من الداتابيز: أول save بيفشل، و create بيصلّحها.

    نفس سلوك الإنتاج: `UpdateError` من الـ UPDATE اللي رجّع صفر صفوف.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.save_calls = 0
        self.create_calls = 0

    def save(self, must_create=False):
        self.save_calls += 1
        if self.create_calls == 0:
            raise UpdateError
        return super().save(must_create=must_create)

    def create(self):
        self.create_calls += 1
        return super().create()


class _HopelessSession(_VanishingRowSession):
    """حتى الإنقاذ بيفشل (الداتابيز واقعة مثلاً)."""

    def create(self):
        raise RuntimeError('db down')


def _existing_key(session):
    """يخلّي الجلسة شكلها جلسة قديمة ليها مفتاح — بس صفّها مش موجود."""
    session._session_key = 'x' * 32
    return session


def _make(middleware_cls, session):
    mw = middleware_cls(lambda r: HttpResponse('ok'))
    request = RequestFactory().get('/system/switch-branch/?branch=1608')
    request.session = session
    return request, mw


class ResilientSessionMiddlewareTests(TestCase):

    def test_stock_middleware_turns_missing_row_into_400(self):
        """توثيق سلوك Django الافتراضي — ده اللي كان بيحصل للمستخدم."""
        session = _existing_key(_VanishingRowSession(None))
        session['active_branch_id'] = 1608
        request, mw = _make(SessionMiddleware, session)

        with self.assertRaises(SessionInterrupted):
            mw.process_response(request, HttpResponse('ok'))

    def test_missing_row_is_recovered_instead_of_400(self):
        session = _existing_key(_VanishingRowSession(None))
        session['_auth_user_id'] = '1'
        session['active_branch_id'] = 1608
        request, mw = _make(ResilientSessionMiddleware, session)

        with self.assertLogs('mouss_tec_core', level='WARNING'):
            response = mw.process_response(request, HttpResponse('ok'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(session.create_calls, 1)
        # البيانات ما ضاعتش والمستخدم فضل داخل
        self.assertEqual(session['active_branch_id'], 1608)
        self.assertEqual(session['_auth_user_id'], '1')
        # والكوكي اتحدّثت بالمفتاح الجديد
        self.assertIn(settings.SESSION_COOKIE_NAME, response.cookies)

    def test_healthy_session_is_untouched(self):
        """المسار العادي ما يتغيّرش: حفظ واحد ومفيش إنشاء جلسة جديدة."""
        session = _VanishingRowSession(None)
        session.create_calls = 1  # من هنا ورايح الحفظ الحقيقي شغال
        session['active_branch_id'] = 7
        session.create()          # صفّ حقيقي في الداتابيز + مفتاح
        creates_before, session.save_calls = session.create_calls, 0
        request, mw = _make(ResilientSessionMiddleware, session)

        response = mw.process_response(request, HttpResponse('ok'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(session.create_calls, creates_before)  # مفيش إنقاذ
        self.assertEqual(session.save_calls, 1)                 # حفظ واحد بس

    def test_recovery_failure_still_returns_the_page(self):
        """لو الإنقاذ نفسه فشل، نرجّع الصفحة بدل ما نطلّع 400."""
        session = _existing_key(_HopelessSession(None))
        session['active_branch_id'] = 1
        request, mw = _make(ResilientSessionMiddleware, session)

        with self.assertLogs('mouss_tec_core', level='ERROR'):
            response = mw.process_response(request, HttpResponse('ok'))
        self.assertEqual(response.status_code, 200)
