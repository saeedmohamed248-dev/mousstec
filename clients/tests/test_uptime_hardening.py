"""
🛡️ «الموقع مايقعش»: فحص الصحة + النشر من غير توقف.

* /healthz/ بيرد على أي دومين (قبل عزل الـ tenants) — Docker والنشر والمراقبة بيستخدموه.
* لو قاعدة البيانات مش بترد ⇒ 503 (فـ autoheal يعمل restart للنسخة دي).
* docker-compose: نسختين ويب بـ healthcheck + خطوة مهاجرات منفصلة + حارس autoheal.
* Caddy بيوزّع على كل النسخ ويعيد الطلب لو نسخة وقفت.
"""
import re
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.test import Client, SimpleTestCase, TestCase, override_settings

ROOT = Path(settings.BASE_DIR)


@override_settings(ALLOWED_HOSTS=['mousstec.com'])
class HealthzTests(TestCase):
    def test_answers_on_any_host_without_tenant_lookup(self):
        r = Client().get('/healthz/', HTTP_HOST='127.0.0.1:8000')   # زي الـ healthcheck من جوه الكونتينر
        self.assertEqual((r.status_code, r.content), (200, b'ok'))
        self.assertEqual(r['Cache-Control'], 'no-store')

    def test_database_down_returns_503(self):
        with mock.patch('erp_core.healthz.connection.cursor', side_effect=Exception('too many clients')):
            r = Client().get('/healthz/', HTTP_HOST='127.0.0.1')
        self.assertEqual(r.status_code, 503)

    def test_is_the_first_middleware(self):
        self.assertEqual(settings.MIDDLEWARE[0], 'erp_core.healthz.HealthzMiddleware')


class DeployTopologyTests(SimpleTestCase):
    def _service(self, name):
        compose = (ROOT / 'docker-compose.yml').read_text(encoding='utf-8')
        return re.search(rf'^  {name}:\n(.*?)(?=^  \w+:\n|^volumes:)', compose, re.S | re.M).group(1)

    def test_web_has_replicas_healthcheck_and_waits_for_migrations(self):
        web = self._service('web')
        self.assertIn('replicas: ${WEB_REPLICAS:-2}', web)
        self.assertIn('/healthz/', web)
        self.assertIn('mousstec.autoheal: "true"', web)
        self.assertIn('service_completed_successfully', web)
        # المهاجرات مابقتش جوه إقلاع الويب (كذا نسخة كانوا هيشغّلوها مع بعض)
        self.assertNotIn('migrate_schemas', (ROOT / 'deploy/entrypoint.web.sh').read_text(encoding='utf-8')
                         .split('RUN_MIGRATIONS')[0])
        self.assertIn('migrate_schemas --shared', (ROOT / 'deploy/entrypoint.migrate.sh').read_text(encoding='utf-8'))

    def test_autoheal_and_postgres_idle_guard(self):
        self.assertIn('health=unhealthy', self._service('autoheal'))
        self.assertIn('idle_session_timeout', self._service('db'))

    def test_caddy_balances_over_all_web_replicas(self):
        caddy = (ROOT / 'deploy/Caddyfile').read_text(encoding='utf-8')
        self.assertIn('dynamic a', caddy)
        self.assertIn('lb_try_duration', caddy)
        self.assertNotIn('reverse_proxy web:8000', caddy)
        self.assertEqual(caddy.count('import web_upstreams'), 4)


class SystemStatusOverallTests(TestCase):
    """«في تدهور أداء» كانت بتظهر لمجرد إن psutil مش متثبتة (فحص «غير معروف»)."""

    def _overall(self, resources):
        from django.contrib.auth.models import User
        from django_tenants.utils import get_tenant_domain_model, get_tenant_model
        Tenant, Domain = get_tenant_model(), get_tenant_domain_model()
        pub = Tenant.objects.filter(schema_name='public').first()
        if pub is None:
            pub = Tenant(schema_name='public', name='Public', owner_name='o', phone='0')
            pub.auto_create_schema = False
            pub.save(verbosity=0)
        Domain.objects.get_or_create(domain='testserver', defaults={'tenant': pub, 'is_primary': False})
        owner = User.objects.create_user('status_owner@test.com', password='x', is_staff=True, is_superuser=True)
        c = Client()
        c.force_login(owner)
        ok = {'status': 'ok', 'name': 'x', 'detail': '', 'icon': 'x'}
        with mock.patch('clients.views.saas_admin_views._check_database', return_value=ok), \
                mock.patch('clients.views.saas_admin_views._check_cache', return_value=ok), \
                mock.patch('clients.views.saas_admin_views._check_celery', return_value=ok), \
                mock.patch('clients.views.saas_admin_views._check_resources', return_value=resources):
            return c.get('/superadmin/system/').context['overall']

    def test_unknown_check_is_not_degraded(self):
        self.assertEqual(self._overall([{'status': 'unknown', 'name': 'Disk', 'detail': '', 'icon': 'x'}]), 'ok')

    def test_real_degradation_still_shows(self):
        self.assertEqual(self._overall([{'status': 'degraded', 'name': 'Disk', 'detail': '', 'icon': 'x'}]), 'degraded')
