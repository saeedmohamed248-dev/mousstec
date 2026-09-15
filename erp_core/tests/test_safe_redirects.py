"""ردود آمنة — مايطلعش للمستخدم "Bad Request (400)" خام.

`HttpResponseRedirect` بيرمي `DisallowedRedirect` (وهي `SuspiciousOperation`)
لأي URL بسكيمة مش http/https/ftp، و Django بيحوّلها لصفحة 400 خام. الهيدر
`Referer` بيتحكم فيه المتصفح بالكامل — المستخدم اللي فتح الموقع من لينك في
واتساب أو من تطبيق ممكن يبعت `whatsapp://` أو `android-app://` — فأي
`redirect(request.META['HTTP_REFERER'])` من غير فحص = صفحة 400 في وشه.
"""
from django.test import RequestFactory, SimpleTestCase

from erp_core.http_utils import safe_back, safe_internal_path


class SafeBackTests(SimpleTestCase):
    def _req(self, referer=None, host='testserver'):
        extra = {'HTTP_HOST': host}
        if referer is not None:
            extra['HTTP_REFERER'] = referer
        return RequestFactory().post('/system/x/', **extra)

    def test_app_scheme_referer_is_dropped(self):
        for ref in ('whatsapp://send?text=hi', 'android-app://com.x/', 'intent://y#Intent;end'):
            self.assertEqual(safe_back(self._req(ref)), '/', ref)

    def test_foreign_host_referer_is_dropped(self):
        self.assertEqual(safe_back(self._req('https://evil.example/p')), '/')

    def test_unknown_host_header_does_not_explode(self):
        """لو الـ Host نفسه مرفوض، get_host() بترمي DisallowedHost —
        الدالة لازم تبلعها وترجّع وجهة آمنة بدل ما تكسر الرد."""
        req = self._req('https://x.invalid/p', host='not-in-allowed-hosts.invalid')
        self.assertEqual(safe_back(req), '/')

    def test_same_host_referer_is_kept(self):
        ref = 'http://testserver/system/dashboard/'
        self.assertEqual(safe_back(self._req(ref)), ref)

    def test_relative_referer_is_kept(self):
        self.assertEqual(safe_back(self._req('/system/dashboard/')), '/system/dashboard/')

    def test_missing_referer_falls_back(self):
        self.assertEqual(safe_back(self._req()), '/')

    def test_header_injection_referer_is_dropped(self):
        self.assertEqual(safe_back(self._req('/x\r\nX-Evil: 1')), '/')


class SafeInternalPathTests(SimpleTestCase):
    def test_accepts_internal_paths(self):
        self.assertEqual(safe_internal_path('/system/a/?q=1'), '/system/a/?q=1')

    def test_rejects_everything_else(self):
        for bad in ('//evil', 'https://evil/x', 'javascript:alert(1)', '', None, '/a\nb'):
            self.assertEqual(safe_internal_path(bad), '/', repr(bad))
