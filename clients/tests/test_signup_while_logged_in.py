"""🔐 تسجيل شركة جديدة وانت داخل بحساب تاني — كان بيقع وكل التسجيل يترجع.

🐛 سجل المراجعة (AuditLog) والـ history (simple-history) بياخدوا المستخدم الحالي من
الـ middleware. الخزنة والفرع اللي بيتعملوا جوه الشركة الجديدة كانوا بيتسجّلوا برقم
مستخدم من schema تانية ⇒ «inventory_auditlog violates foreign key … auth_user»
والشركة ماتتعملش (السيرفر: SaaS PROVISIONING CRASH).
"""
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client as HttpClient, override_settings
from django_tenants.utils import schema_context

from clients.models import Client
from inventory.tests.base import ERPTenantTestCase, delete_test_tenant

User = get_user_model()

BASE = {'company_name': 'ورشة الدخول', 'full_name': 'سعيد محمد', 'email': 'owner@loggedin.test',
        'phone': '01094850763', 'password': 'Str0ng#Pass2026', 'website_url_honeypot': '',
        'subdomain': 'logged-in-shop', 'industry': 'automotive', 'business_type': 'service_center'}


@override_settings(ALLOWED_HOSTS=['*'])
class SignupWhileLoggedInTests(ERPTenantTestCase):

    def setUp(self):
        super().setUp()
        from django.core.cache import cache
        cache.delete('signup_ip:127.0.0.1')
        connection.set_tenant(self.tenant)
        # رقم مستخدم مش هيبقى موجود في الشركة الجديدة (فيها المالك بس)
        self.me = User(id=987654, username='already_in', is_staff=True)
        self.me.set_password('x')
        self.me.save()
        self.c = HttpClient()
        self.c.force_login(self.me)
        connection.set_schema_to_public()

    def tearDown(self):
        connection.set_schema_to_public()
        for t in Client.all_objects.filter(email='owner@loggedin.test'):
            delete_test_tenant(t, None)
        super().tearDown()

    def test_signup_succeeds_and_new_company_records_have_no_foreign_user(self):
        r = self.c.post('/connect/signup/', BASE, HTTP_HOST=self.domain.domain)
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, 'تعذر بناء مساحة العمل')
        tenant = Client.all_objects.get(email='owner@loggedin.test')
        with schema_context(tenant.schema_name):
            from inventory.models import AuditLog, Treasury
            self.assertTrue(User.objects.filter(email='owner@loggedin.test', is_superuser=True).exists())
            self.assertFalse(AuditLog.objects.filter(user_id=987654).exists())
            self.assertFalse(Treasury.history.filter(history_user_id=987654).exists())


class AuditUserSchemaRuleTests(ERPTenantTestCase):
    """الشغل العادي جوه الشركة لازم يفضل يسجّل اسم المستخدم — بس من نفس الـ schema."""

    def test_same_schema_keeps_user_other_schema_drops_it(self):
        from erp_core.middleware import _audit_thread_local, audit_user_for_current_schema
        connection.set_tenant(self.tenant)
        user = User.objects.create_user(username='auditor_same', password='x')
        _audit_thread_local.user, _audit_thread_local.schema = user, self.tenant.schema_name
        try:
            self.assertEqual(audit_user_for_current_schema(), user)
            connection.set_schema_to_public()
            self.assertIsNone(audit_user_for_current_schema())
        finally:
            _audit_thread_local.user = _audit_thread_local.schema = None
            connection.set_schema_to_public()

    def test_audit_log_inside_own_company_records_the_user(self):
        from erp_core.middleware import _audit_thread_local
        from inventory.models import AuditLog, Branch, Treasury
        connection.set_tenant(self.tenant)
        user = User.objects.create_user(username='auditor_tr', password='x')
        _audit_thread_local.user, _audit_thread_local.schema = user, self.tenant.schema_name
        try:
            t = Treasury.objects.create(name='خزنة المراجعة', branch=Branch.objects.first()
                                        or Branch.objects.create(name='فرع المراجعة'))
            self.assertTrue(AuditLog.objects.filter(model_name='Treasury', object_id=str(t.pk),
                                                    user=user).exists())
            self.assertEqual(t.history.first().history_user_id, None)   # من غير request: simple-history مالوش يوزر
        finally:
            _audit_thread_local.user = _audit_thread_local.schema = None
            connection.set_schema_to_public()
