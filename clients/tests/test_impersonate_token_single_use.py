"""🔐 توكن «دخول كأدمن الشركة» (impersonate-login) لازم يتستخدم مرة واحدة بس.

قبل كده: التوكن الموقّع كان صالح لأي عدد مرات طول الـ 120 ثانية — لو اتسرّب
(لوج السيرفر / Referer / تاريخ المتصفح) أي حد يقدر يدخل بيه كأدمن الشركة.
"""
from django.contrib.auth import get_user_model
from django.core import signing
from django.core.cache import cache
from django.test import Client as HttpClient, override_settings

from inventory.tests.base import ERPTenantTestCase

User = get_user_model()


@override_settings(ALLOWED_HOSTS=['*'])
class ImpersonateTokenSingleUseTests(ERPTenantTestCase):

    def setUp(self):
        super().setUp()
        cache.clear()
        User.objects.create_superuser('owner1', 'owner@shop.test', 'x-Strong-pass-1')

    def _token(self):
        return signing.dumps({'schema_name': self.tenant.schema_name, 'superuser_id': 1,
                              'superuser_name': 'root', 'created': 0},
                             salt='impersonate-login-token')

    def test_token_works_once_then_is_rejected(self):
        token = self._token()
        first = HttpClient().get('/impersonate-login/', {'token': token}, HTTP_HOST=self.domain.domain)
        self.assertEqual(first.status_code, 302)
        self.assertNotIn('login', first['Location'])
        self.assertIn('_auth_user_id', first.client.session)

        replay = HttpClient()
        second = replay.get('/impersonate-login/', {'token': token}, HTTP_HOST=self.domain.domain)
        self.assertEqual(second.status_code, 302)
        self.assertIn('token_expired', second['Location'])
        self.assertNotIn('_auth_user_id', replay.session)
