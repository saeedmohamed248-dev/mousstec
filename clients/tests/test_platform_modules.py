"""🧩 مفاتيح تشغيل الموديولات — المالك يقفل موديول مش محتاجه من غير ما يمسح كوده ولا بياناته.

قبل كده مفيش طريقة توقف موديول (زي برمجة BMW) غير إنك تشيل الكود — ومفيش أي رقم يقول
مين بيستخدم إيه عشان قرار التركيز يبقى مبني على حاجة.
"""
from django.contrib.auth import get_user_model
from django.test import Client as HttpClient, TestCase, override_settings

from clients.models import PlatformModule
from clients.services import modules
from marketplace_c2c.tests.test_parts_order_lifecycle import _PublicDomainMixin

User = get_user_model()


@override_settings(ALLOWED_HOSTS=['*'])
class PlatformModuleSwitchTests(_PublicDomainMixin, TestCase):

    def setUp(self):
        modules.clear_cache()
        self.addCleanup(modules.clear_cache)
        self.owner = User.objects.create_user('owner@plat.test', password='x', is_superuser=True, is_staff=True)
        self.c = HttpClient()
        self.c.force_login(self.owner)

    def test_bmw_programming_is_off_after_the_migration_and_its_links_are_gone(self):
        self.assertFalse(modules.is_enabled('bmw_ecu'))
        r = HttpClient().get('/bmw-ecu/storefront/')
        self.assertEqual(r.status_code, 404)
        self.assertContains(r, 'الميزة دي متوقفة حالياً', status_code=404)
        r = HttpClient().post('/api/ecu/session/start/', content_type='application/json')
        self.assertEqual((r.status_code, r.json()['error']), (404, 'module_off'))
        self.assertIn('bmw_ecu', modules.disabled_app_labels())        # مش ظاهر في قايمة لوحة الشركة

    def test_owner_turns_a_module_off_and_on_again(self):
        self.assertNotEqual(HttpClient().get('/robot/').status_code, 404)
        self.c.post('/superadmin/modules/', {'code': 'robot', 'action': 'disable', 'note': 'مفيش عملاء'})
        row = PlatformModule.objects.get(code='robot')
        self.assertEqual((row.is_enabled, row.note, row.updated_by), (False, 'مفيش عملاء', self.owner))
        self.assertEqual(HttpClient().get('/robot/').status_code, 404)
        from robot.tasks import raise_offline_alerts
        self.assertEqual(raise_offline_alerts(), {'skipped': 'module robot is off'})   # المهمة المجدولة وقفت
        self.c.post('/superadmin/modules/', {'code': 'robot', 'action': 'enable'})
        self.assertNotEqual(HttpClient().get('/robot/').status_code, 404)

    def test_page_lists_every_optional_module_with_its_usage(self):
        r = self.c.get('/superadmin/modules/')
        self.assertEqual(r.status_code, 200)
        for spec in modules.MODULES.values():
            self.assertContains(r, spec['name'])
        self.assertEqual({m['code'] for m in r.context['modules']}, set(modules.MODULES))

    def test_core_paths_are_never_switchable_and_unknown_codes_are_ignored(self):
        for path in ('/system/dashboard/', '/printing/', '/hr/attendance/', '/pricing/', '/superadmin/'):
            self.assertIsNone(modules.module_for_path(path), path)
        self.c.post('/superadmin/modules/', {'code': 'inventory', 'action': 'disable'})
        self.assertFalse(PlatformModule.objects.filter(code='inventory').exists())

    def test_only_the_platform_owner_can_switch_modules(self):
        from clients.models import StaffRole
        agent = User.objects.create_user('agent@plat.test', password='x', is_staff=True)
        StaffRole.objects.create(user=agent, role='tech_admin')
        c = HttpClient()
        c.force_login(agent)
        self.assertNotEqual(c.get('/superadmin/modules/').status_code, 200)
        c.post('/superadmin/modules/', {'code': 'social_ads', 'action': 'disable'})
        self.assertTrue(modules.is_enabled('social_ads'))
