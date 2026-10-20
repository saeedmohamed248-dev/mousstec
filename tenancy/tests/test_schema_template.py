"""⚡ تأسيس الشركة من قالب الـ schema (tenancy/services/schema_template.py).

الشركة الجديدة بتتنسخ من قالب متهاجر بدل تشغيل كل المهاجرات (~٥٠ ثانية).
اللي لازم يفضل صح: النسخة = نفس اللي المهاجرات بتعمله، والقالب القديم مايبوّظش
شركة جديدة (الـ migrate بعد النسخ حقيقي مش fake)، ومن غير قالب التأسيس العادي شغّال.
"""
import uuid

from django.db import connection
from django.test import TransactionTestCase, override_settings
from django_tenants.utils import get_tenant_domain_model, get_tenant_model, schema_exists

from inventory.tests.base import delete_test_tenant
from tenancy.services.schema_template import ensure_template, template_schema_name


def _count(sql, *args):
    with connection.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchone()[0]


def _tables(schema):
    return _count("SELECT count(*) FROM information_schema.tables WHERE table_schema=%s", schema)


def _migrations(schema):
    return _count(f'SELECT count(*) FROM "{schema}".django_migrations')


class SchemaTemplateTests(TransactionTestCase):

    def setUp(self):
        connection.set_schema_to_public()
        self.created = []

    def tearDown(self):
        for tenant, domain in self.created:
            delete_test_tenant(tenant, domain)
        connection.set_schema_to_public()

    def _new_tenant(self):
        suffix = uuid.uuid4().hex[:6]
        tenant = get_tenant_model()(schema_name=f'tpl_{suffix}', name='Template Co',
                                    owner_name='Owner', phone='01000000000')
        tenant.auto_create_schema = True
        tenant.save(verbosity=0)
        domain = get_tenant_domain_model().objects.create(
            tenant=tenant, domain=f'tpl-{suffix}.test.com', is_primary=True)
        self.created.append((tenant, domain))
        connection.set_schema_to_public()
        return tenant

    def test_new_tenant_matches_the_template(self):
        tenant = self._new_tenant()
        template = template_schema_name()
        self.assertTrue(schema_exists(template))
        self.assertEqual(_tables(tenant.schema_name), _tables(template))
        self.assertEqual(_migrations(tenant.schema_name), _migrations(template))
        # الشركة شغالة: إضافة صف جديد بعد بيانات المهاجرات من غير تصادم أرقام
        connection.set_tenant(tenant)
        from django.contrib.contenttypes.models import ContentType
        ct = ContentType.objects.create(app_label='zz_template_probe', model='probe')
        self.assertGreater(ct.pk, 1)

    def test_stale_template_is_completed_not_faked(self):
        """قالب ناقصه مهاجرة (نشر جديد قبل تحديثه) ⇒ الشركة الجديدة بتاخد المهاجرة فعلاً."""
        template = ensure_template()
        with connection.cursor() as cur:
            cur.execute(f'DROP TABLE "{template}".robot_robotentry')
            cur.execute(f'DELETE FROM "{template}".django_migrations '
                        "WHERE app='robot' AND name='0009_robot_entry'")
        try:
            tenant = self._new_tenant()
            self.assertEqual(_count("SELECT count(*) FROM information_schema.tables "
                                    "WHERE table_schema=%s AND table_name='robot_robotentry'",
                                    tenant.schema_name), 1)
            self.assertEqual(_count(f'SELECT count(*) FROM "{tenant.schema_name}".django_migrations '
                                    "WHERE app='robot' AND name='0009_robot_entry'"), 1)
        finally:
            ensure_template()          # القالب يرجع كامل لباقي الاختبارات
        self.assertEqual(_count("SELECT count(*) FROM information_schema.tables "
                                "WHERE table_schema=%s AND table_name='robot_robotentry'", template), 1)

    @override_settings(TENANT_TEMPLATE_SCHEMA='')
    def test_without_a_template_tenants_are_built_by_migrations(self):
        tenant = self._new_tenant()
        self.assertGreater(_tables(tenant.schema_name), 100)
        self.assertGreater(_migrations(tenant.schema_name), 100)

    def test_template_schema_name_is_reserved(self):
        """شركة بنفس اسم القالب ماتاخدش القالب — والقالب مايتمسحش."""
        template = ensure_template()
        tenant = get_tenant_model()(schema_name=template, name='Tenant Template',
                                    owner_name='Owner', phone='01000000000')
        with self.assertRaisesMessage(ValueError, 'already exists'):
            tenant.save(verbosity=0)
        self.assertTrue(schema_exists(template))
        self.assertFalse(get_tenant_model().objects.filter(schema_name=template).exists())

        from clients.forms import TenantSignupForm
        form = TenantSignupForm(data={'subdomain': template.replace('_', '-')})
        form.is_valid()
        self.assertIn('subdomain', form.errors)
