"""☑️ مركز الأخطاء (حل جماعي + فلاتر + تصدير) و🛡️ مركز الأمان (حظر IP، فك قفل الدخول)."""
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client, TestCase, override_settings

from clients.models import BlockedIP, Client as Tenant, Domain, SystemErrorLog

HOST = 'platform-sec.test.com'


def _public():
    pub = Tenant.objects.filter(schema_name='public').first()
    if pub is None:
        pub = Tenant(schema_name='public', name='Platform', owner_name='o', phone='0')
        pub.auto_create_schema = False
        pub.save()
    Domain.objects.get_or_create(domain=HOST, defaults={'tenant': pub, 'is_primary': False})

User = get_user_model()


@override_settings(ALLOWED_HOSTS=['*'])
class ErrorsCenterTests(TestCase):
    def setUp(self):
        connection.set_schema_to_public()
        _public()
        self.boss = User.objects.create_superuser('err_boss', 'eb@x.com', 'x')
        self.c = Client(HTTP_HOST=HOST)
        self.c.force_login(self.boss)

    def _err(self, **kw):
        d = dict(tenant_schema='shop_a', tenant_name='A', path='/x/', method='GET', status_code=500,
                 level='critical', fingerprint='fp1')
        d.update(kw)
        return SystemErrorLog.objects.create(**d)

    def test_bulk_resolve_selected_closes_its_whole_group_only(self):
        a1, a2 = self._err(), self._err()
        other = self._err(fingerprint='fp2', path='/y/')
        r = self.c.post('/superadmin/errors/bulk/', {'action': 'resolve', 'ids': [a1.pk], 'next': '/superadmin/errors/'})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(SystemErrorLog.objects.get(pk=a2.pk).is_resolved)
        self.assertFalse(SystemErrorLog.objects.get(pk=other.pk).is_resolved)
        self.c.post('/superadmin/errors/bulk/', {'action': 'reopen', 'ids': [a1.pk]})
        self.assertFalse(SystemErrorLog.objects.get(pk=a2.pk).is_resolved)

    def test_resolve_filter_respects_filter_and_refuses_if_new_errors_arrived(self):
        self._err(status_code=404, level='warning', fingerprint='n1')
        keep = self._err(status_code=500, fingerprint='s1')
        r = self.c.post('/superadmin/errors/bulk/', {'action': 'resolve_filter', 'filter_qs': 'code=404', 'expected': '0'})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(SystemErrorLog.objects.filter(status_code=404, is_resolved=False).count(), 1)
        self.c.post('/superadmin/errors/bulk/', {'action': 'resolve_filter', 'filter_qs': 'code=404', 'expected': '1'})
        self.assertEqual(SystemErrorLog.objects.filter(status_code=404, is_resolved=False).count(), 0)
        self.assertFalse(SystemErrorLog.objects.get(pk=keep.pk).is_resolved)

    def test_page_filters_and_csv(self):
        self._err(path='/needle/', ip_address='8.8.8.8')
        r = self.c.get('/superadmin/errors/?q=needle&code=5xx&window=24h')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context['match_total'], 1)
        self.assertEqual(self.c.get('/superadmin/errors/?q=8.8.8.8').context['match_total'], 1)
        csv = self.c.get('/superadmin/errors/?export=csv')
        self.assertIn('/needle/', csv.content.decode('utf-8'))


@override_settings(ALLOWED_HOSTS=['*'])
class SecurityCenterTests(TestCase):
    def setUp(self):
        connection.set_schema_to_public()
        _public()
        self.boss = User.objects.create_superuser('sec_boss', 'sb@x.com', 'x')
        self.c = Client(REMOTE_ADDR='9.9.9.9', HTTP_HOST=HOST)
        self.c.force_login(self.boss)

    def test_page_renders(self):
        self.assertEqual(self.c.get('/superadmin/security/').status_code, 200)

    def test_block_then_requests_get_403_then_unblock(self):
        from erp_core.ip_block import invalidate
        self.c.post('/superadmin/security/action/', {'action': 'block', 'ip': '45.1.2.3', 'duration': '24h'})
        self.assertTrue(BlockedIP.objects.filter(ip_address='45.1.2.3').exists())
        invalidate()
        self.assertEqual(Client(REMOTE_ADDR='45.1.2.3', HTTP_HOST=HOST).get('/').status_code, 403)
        self.c.post('/superadmin/security/action/', {'action': 'unblock', 'ip': '45.1.2.3'})
        self.assertNotEqual(Client(REMOTE_ADDR='45.1.2.3', HTTP_HOST=HOST).get('/').status_code, 403)

    def test_cannot_block_self_or_private(self):
        self.c.post('/superadmin/security/action/', {'action': 'block', 'ip': '9.9.9.9'})
        self.c.post('/superadmin/security/action/', {'action': 'block', 'ip': '10.0.0.5'})
        self.assertFalse(BlockedIP.objects.exists())

    def test_non_admin_forbidden(self):
        u = User.objects.create_user('plain', 'p@x.com', 'x')
        c = Client(HTTP_HOST=HOST); c.force_login(u)
        self.assertNotEqual(c.get('/superadmin/security/').status_code, 200)
