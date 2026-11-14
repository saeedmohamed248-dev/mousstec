"""
🧭 رحلة الشركة في السوبر أدمن (Tenant 360):
* الصفحات بالترتيب ومتقسّمة لزيارات، وآخر صفحة وقفت عندها، وعلامة «أونلاين دلوقتي».
* مسارات الأدمن بتتحوّل لأسماء مفهومة («حفظ إضافة …»).
* «مين عمل إيه» جاي من سجل الأدمن جوه schema الشركة (اسم الموظف + السجل).
"""
from datetime import timedelta

from django.contrib.admin.models import ADDITION, LogEntry
from django.contrib.auth.models import User
from django.contrib.contenttypes.models import ContentType
from django.db import connection
from django.test import Client as HttpClient, override_settings
from django.utils import timezone
from django_tenants.utils import schema_context

from clients.models import Client, Domain, VisitorLog
from clients.services.tenant_journey import build_tenant_journey, describe
from .base import ERPTenantTestCase

PLATFORM_HOST = 'platform-tj.test.com'


@override_settings(ALLOWED_HOSTS=['*'])
class TenantJourneyTests(ERPTenantTestCase):
    def setUp(self):
        connection.set_schema_to_public()
        pub = Client.objects.filter(schema_name='public').first()
        if pub is None:
            pub = Client(schema_name='public', name='Platform', owner_name='o', phone='0')
            pub.auto_create_schema = False
            pub.save()
        Domain.objects.get_or_create(domain=PLATFORM_HOST, defaults={'tenant': pub, 'is_primary': False})
        self.owner, _ = User.objects.get_or_create(
            username='tj_owner@test.com', defaults={'is_staff': True, 'is_superuser': True})
        # زيارات اتسجّلت وقت تجهيز الشركة — مش جزء من السيناريو
        VisitorLog.objects.filter(tenant_schema=self.tenant.schema_name).delete()

    def tearDown(self):
        connection.set_tenant(self.tenant)
        super().tearDown()

    def _log(self, path, minutes_ago, method='GET', status=200):
        v = VisitorLog.objects.create(path=path, method=method, status_code=status,
                                      tenant_schema=self.tenant.schema_name, device_type='desktop')
        VisitorLog.objects.filter(pk=v.pk).update(timestamp=timezone.now() - timedelta(minutes=minutes_ago))

    def test_describe_admin_paths(self):
        name = str(User._meta.verbose_name)
        self.assertEqual(describe('/secure-portal/auth/user/add/', 'POST'), f'حفظ إضافة {name}')
        self.assertEqual(describe('/secure-portal/auth/user/'), f'قائمة {name}')
        self.assertEqual(describe('/secure-portal/auth/user/5/delete/', 'POST'), f'تأكيد حذف {name} #5')
        self.assertEqual(describe('/nothing-known/'), '')

    def test_sessions_last_page_online_and_actions(self):
        self._log('/secure-portal/', 200)          # زيارة قديمة
        self._log('/secure-portal/auth/user/', 3)  # زيارة جديدة
        self._log('/secure-portal/auth/user/add/', 2, method='POST', status=302)
        self._log('/secure-portal/auth/user/', 1)
        with schema_context(self.tenant.schema_name):
            emp = User.objects.create(username='cashier1', first_name='أحمد')
            LogEntry.objects.create(user=emp, content_type=ContentType.objects.get_for_model(User),
                                    object_id=str(emp.pk), object_repr='cashier2',
                                    action_flag=ADDITION, change_message='')

        j = build_tenant_journey(self.tenant, days=7)
        self.assertEqual(len(j['sessions']), 2)
        self.assertTrue(j['online'])
        self.assertEqual(j['last_page']['path'], '/secure-portal/auth/user/')
        newest = j['sessions'][0]
        self.assertEqual(newest['page_count'], 3)
        self.assertEqual(newest['action_count'], 1)
        self.assertEqual(newest['pages'][0]['seconds'], 60)
        self.assertEqual(j['actions'][0]['user'], 'أحمد')
        self.assertEqual(j['actions'][0]['verb'], 'أضاف')
        self.assertEqual(j['actions'][0]['object'], 'cashier2')

        c = HttpClient(); c.force_login(self.owner)
        r = c.get(f'/superadmin/tenant/{self.tenant.pk}/360/?journey_days=1', HTTP_HOST=PLATFORM_HOST)
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        self.assertIn('رحلة الشركة على السيستم', html)
        self.assertIn('cashier2', html)
        self.assertIn(f'حفظ إضافة {User._meta.verbose_name}', html)

    def test_offline_when_last_visit_is_old(self):
        self._log('/secure-portal/', 20)
        self.assertFalse(build_tenant_journey(self.tenant)['online'])

    def test_tenants_list_links_to_360(self):
        c = HttpClient(); c.force_login(self.owner)
        r = c.get('/superadmin/tenants/', HTTP_HOST=PLATFORM_HOST)
        self.assertEqual(r.status_code, 200)
        self.assertIn(f'/superadmin/tenant/{self.tenant.pk}/360/', r.content.decode())
