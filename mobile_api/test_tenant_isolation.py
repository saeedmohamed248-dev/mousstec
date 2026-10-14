"""
🏢 فصل الشركات: دخول شركة مايفتحش شركة تانية.

كل شركة ليها schema وجدول مستخدمين لوحدها، فرقم المستخدم (1، 2، ...) بيتكرّر
بين الشركات. أي حاجة بتعرّف المستخدم برقمه بس (توكن الموبايل، الجلسة) لازم
تتأكّد إنها اتعملت في نفس الشركة — وإلا مستخدم رقم 1 في شركة (أي حد يسجّل
شركة جديدة) يدخل كمستخدم رقم 1 (صاحب الشركة غالباً) في شركة تانية.
"""
import uuid

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client, override_settings
from django_tenants.utils import get_tenant_domain_model, get_tenant_model, schema_context
from rest_framework.test import APIClient

from inventory.tests.base import ERPTenantTestCase

User = get_user_model()


@override_settings(ALLOWED_HOSTS=['*'])
class CrossTenantIsolationTests(ERPTenantTestCase):
    """شركة «أ» (المهاجم — سجّل شركة عادي) وشركة «ب» (الضحية)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        connection.set_schema_to_public()
        sfx = uuid.uuid4().hex[:6]
        cls.victim = get_tenant_model()(schema_name=f'test_{sfx}v', name='Victim Co',
                                        owner_name='Victim', phone='01000000001')
        cls.victim.auto_create_schema = True
        cls.victim.save(verbosity=0)
        cls.victim_domain = get_tenant_domain_model()(
            tenant=cls.victim, domain=f'test-{sfx}v.test.com', is_primary=True)
        cls.victim_domain.save()
        connection.set_tenant(cls.tenant)

    @classmethod
    def tearDownClass(cls):
        connection.set_schema_to_public()
        for obj in (getattr(cls, 'victim_domain', None), getattr(cls, 'victim', None)):
            try:
                if obj is cls.victim:
                    obj.delete(force_drop=True)
                elif obj is not None:
                    obj.delete()
            except Exception:
                pass
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        self.attacker_host = self.domain.domain
        self.victim_host = self.victim_domain.domain
        # نفس رقم المستخدم في الشركتين (ده اللي بيحصل طبيعي: كل schema بتبدأ من 1)
        with schema_context(self.victim.schema_name):
            # جداول الشركتين مش بتتنضّف بين الاختبارات — أسماء فريدة لكل اختبار
            self.owner = User.objects.create_user(f'victim_owner_{uuid.uuid4().hex[:6]}',
                                                  password='Victim-pass-123')
            self.owner.is_superuser = True
            self.owner.save()
            victim_pk = self.owner.pk
        self.attacker_name = f'attacker_{uuid.uuid4().hex[:6]}'
        with schema_context(self.tenant.schema_name):
            attacker = User.objects.create_user(self.attacker_name, password='Attacker-pass-123')
            while attacker.pk < victim_pk:
                attacker.delete()
                attacker = User.objects.create_user(self.attacker_name, password='Attacker-pass-123')
            self.attacker = attacker
        with schema_context(self.victim.schema_name):
            while User.objects.order_by('-pk').first().pk < self.attacker.pk:
                User.objects.create_user(f'filler{uuid.uuid4().hex[:6]}', password='x-Filler-123')
            self.victim_same_pk = User.objects.get(pk=self.attacker.pk)
        connection.set_tenant(self.tenant)

    def _login_attacker(self):
        api = APIClient()
        r = api.post('/api/mobile/v1/auth/login/',
                     {'username': self.attacker_name, 'password': 'Attacker-pass-123'},
                     format='json', HTTP_HOST=self.attacker_host)
        self.assertEqual(r.status_code, 200, r.content)
        return r.data

    def test_mobile_token_from_one_company_is_rejected_by_another(self):
        tokens = self._login_attacker()
        api = APIClient()
        api.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")
        # على شركته: شغّال
        self.assertEqual(api.get('/api/mobile/v1/auth/me/', HTTP_HOST=self.attacker_host).status_code, 200)
        # على الشركة التانية: مرفوض (كان بيرجع بيانات مستخدم الشركة التانية بنفس الرقم)
        r = api.get('/api/mobile/v1/auth/me/', HTTP_HOST=self.victim_host)
        self.assertEqual(r.status_code, 401, getattr(r, 'data', r.content))
        r = api.get('/api/mobile/v1/treasuries/', HTTP_HOST=self.victim_host)
        self.assertEqual(r.status_code, 401)

    def test_refresh_token_from_one_company_is_rejected_by_another(self):
        tokens = self._login_attacker()
        r = APIClient().post('/api/mobile/v1/auth/refresh/', {'refresh': tokens['refresh']},
                             format='json', HTTP_HOST=self.victim_host)
        self.assertEqual(r.status_code, 401, getattr(r, 'data', r.content))
        # على شركته: التجديد شغّال
        r = APIClient().post('/api/mobile/v1/auth/refresh/', {'refresh': tokens['refresh']},
                             format='json', HTTP_HOST=self.attacker_host)
        self.assertEqual(r.status_code, 200, getattr(r, 'data', r.content))

    def test_session_cookie_from_one_company_does_not_log_into_another(self):
        with schema_context(self.tenant.schema_name):
            own = Client(HTTP_HOST=self.attacker_host)
            own.force_login(self.attacker)
        stolen = own.cookies['mt_sessionid'].value
        other = Client(HTTP_HOST=self.victim_host)
        other.cookies['mt_sessionid'] = stolen
        r = other.get('/system/dashboard/')
        self.assertIn(r.status_code, (301, 302), r.status_code)
        self.assertIn('login', r['Location'])

    def test_private_files_need_login_role_and_the_owning_company(self):
        import tempfile
        from django.core.files.base import ContentFile
        from inventory.models import BankStatement
        from inventory.tests import factories as f

        with tempfile.TemporaryDirectory() as media, override_settings(MEDIA_ROOT=media):
            with schema_context(self.victim.schema_name):
                branch = f.make_branch(name='فرع الضحية')
                till = f.make_treasury(branch, name='بنك الضحية', balance='0.00')
                stmt = BankStatement(treasury=till, statement_date='2026-03-31', period_start='2026-03-01',
                                     period_end='2026-03-31', opening_balance=0, closing_balance=0)
                stmt.uploaded_file.save('march.csv', ContentFile(b'SECRET-BANK-DATA'), save=True)
                url = stmt.uploaded_file.url
                cashier = User.objects.create_user(f'cashier_{uuid.uuid4().hex[:6]}', password='x-Cashier-123')
                cashier.employee_profile.role = 'cashier'
                cashier.employee_profile.save()
                owner_client = Client(HTTP_HOST=self.victim_host)
                owner_client.force_login(self.owner)
                cashier_client = Client(HTTP_HOST=self.victim_host)
                cashier_client.force_login(cashier)
            with schema_context(self.tenant.schema_name):
                self.attacker.employee_profile.role = 'admin'
                self.attacker.employee_profile.save()
                attacker_client = Client(HTTP_HOST=self.attacker_host)
                attacker_client.force_login(self.attacker)

            self.assertTrue(url.startswith('/media/bank_statements/'), url)
            # من غير دخول ← صفحة الدخول
            r = Client(HTTP_HOST=self.victim_host).get(url)
            self.assertEqual(r.status_code, 302)
            self.assertIn('/login/', r['Location'])
            # الكاشير مالوش صلاحية على كشوف البنك
            self.assertEqual(cashier_client.get(url).status_code, 403)
            # صاحب الشركة بيفتحه
            r = owner_client.get(url)
            self.assertEqual(r.status_code, 200)
            self.assertEqual(b''.join(r.streaming_content), b'SECRET-BANK-DATA')
            # أدمن شركة تانية: الملف مش موجود بالنسبة له (حتى لو عارف المسار)
            self.assertEqual(attacker_client.get(url).status_code, 404)
            # محاولة الالتفاف على المسار
            r = owner_client.get('/media/bank_statements/../../etc/passwd')
            self.assertIn(r.status_code, (301, 302, 404))
            self.assertFalse(hasattr(r, 'streaming_content'))

    def test_company_admin_panel_hides_platform_wide_tables(self):
        """صاحب الشركة superuser في الأدمن بتاعه — مايشوفش جداول المنصة المشتركة."""
        import os
        admin_url = os.getenv('ADMIN_URL', 'secure-portal')
        with schema_context(self.victim.schema_name):
            self.owner.is_staff = True
            self.owner.save()
            owner = Client(HTTP_HOST=self.victim_host)
            owner.force_login(self.owner)
        # جداول الشركة نفسها شغّالة عادي في الأدمن بتاعها
        self.assertEqual(owner.get(f'/{admin_url}/inventory/customer/').status_code, 200)
        for path in ('social_ads/socialadsconfig/', 'omnichannel/tenantchannelconfig/',
                     'omnichannel/channelmessagelog/', 'django_celery_beat/periodictask/'):
            r = owner.get(f'/{admin_url}/{path}')
            self.assertIn(r.status_code, (302, 403, 404), path)
        index = owner.get(f'/{admin_url}/')
        if index.status_code == 200:
            self.assertNotContains(index, 'socialadsconfig')
            self.assertNotContains(index, 'periodictask')
