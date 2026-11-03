"""
🔁 دورة الاشتراك: اللي اتلقى في مراجعة الاشتراكات.

* «حذف الشركة نهائياً» من لوحة المشرف ماكانش في transaction: الروابط بتتمسح والـ schema
  بيتعمله DROP وبعدين مسح الصف يقع (فواتير الاشتراك PROTECT) ⇒ شركة متسجّلة من غير
  بيانات ولا رابط، ومهاجرات كل نشر تقع عليها (MigrationSchemaMissing) — حصلت فعلاً.
* مهمة التعليق/التحذير اليومية بتشتغل 12:05 بالليل بتوقيت القاهرة وكانت بتحسب «النهارده»
  بتاريخ UTC (يوم امبارح) ⇒ التعليق متأخر يوم والتحذيرات بأيام غلط.
"""
from __future__ import annotations

import datetime as dt
import uuid
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.db import connection
from django.test import Client, TestCase

from clients.models import Client as Tenant, Domain, Plan, PlatformInvoice
from clients.services.platform_billing import issue_subscription_invoice


def _tenant(**extra):
    schema = f'tst_{uuid.uuid4().hex[:8]}'
    fields = {'name': 'شركة اختبار', 'owner_name': 'o', 'phone': '01000000000', 'industry': 'automotive', **extra}
    Tenant.objects.bulk_create([Tenant(schema_name=schema, **fields)])
    return Tenant.objects.get(schema_name=schema)


def _schema_exists(name):
    with connection.cursor() as c:
        c.execute('select 1 from information_schema.schemata where schema_name=%s', [name])
        return c.fetchone() is not None


class SuperAdminDeleteTenantTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        pub = Tenant.objects.filter(schema_name='public').first()
        cls._made_pub = pub is None
        if cls._made_pub:
            pub = Tenant(schema_name='public', name='Public', owner_name='o', phone='0')
            pub.auto_create_schema = False
            pub.save(verbosity=0)
        cls._pub = pub
        cls._dom = None
        if not Domain.objects.filter(domain='testserver').exists():
            cls._dom = Domain.objects.create(tenant=pub, domain='testserver', is_primary=False)

    @classmethod
    def tearDownClass(cls):
        if cls._dom is not None:
            cls._dom.delete()
        if cls._made_pub:
            cls._pub.delete(force_drop=False)
        super().tearDownClass()

    def setUp(self):
        patcher = mock.patch.object(Tenant, 'auto_create_schema', False)
        patcher.start()
        self.addCleanup(patcher.stop)
        owner = User.objects.create_user('del_owner@test.com', password='x', is_staff=True, is_superuser=True)
        self.c = Client()
        self.c.force_login(owner)
        self.tenant = _tenant()
        with connection.cursor() as cur:
            cur.execute(f'CREATE SCHEMA "{self.tenant.schema_name}"')
        Domain.objects.create(tenant=self.tenant, domain=f'{self.tenant.schema_name}.example.com', is_primary=True)

    def _delete(self):
        return self.c.post('/superadmin/', {'action': 'delete_tenant', 'tenant_id': self.tenant.pk,
                                            'confirm_name': self.tenant.schema_name}, follow=True)

    def test_tenant_with_invoices_is_left_untouched(self):
        plan, _ = Plan.objects.get_or_create(slug='del-silver', defaults={
            'name': 'Silver', 'industry': 'automotive', 'monthly_price': Decimal('850.00'), 'is_active': True})
        issue_subscription_invoice(self.tenant, plan, 1, paid=False)

        r = self._delete()

        self.assertTrue(Tenant.objects.filter(pk=self.tenant.pk).exists())
        self.assertTrue(_schema_exists(self.tenant.schema_name), 'الـ schema اتمسح والشركة فضلت')
        self.assertTrue(Domain.objects.filter(tenant=self.tenant).exists(), 'الرابط اتمسح والشركة فضلت')
        self.assertEqual(PlatformInvoice.objects.filter(tenant=self.tenant).count(), 1)
        self.assertContains(r, 'مش هينفع تتمسح')

    def test_tenant_without_financial_records_is_fully_deleted(self):
        r = self._delete()

        self.assertFalse(Tenant.objects.filter(pk=self.tenant.pk).exists())
        self.assertFalse(_schema_exists(self.tenant.schema_name))
        self.assertContains(r, 'نهائياً')


class BillingCronUsesCairoDateTests(TestCase):
    """12:05 بالليل في القاهرة (11 يناير) = 10:05 مساءً UTC يوم 10."""

    CAIRO_TODAY = dt.date(2027, 1, 11)
    NOW_UTC = dt.datetime(2027, 1, 10, 22, 5, tzinfo=dt.timezone.utc)

    def setUp(self):
        patcher = mock.patch.object(Tenant, 'auto_create_schema', False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run(self):
        from clients.tasks import orchestrate_billing_and_suspensions
        with mock.patch('django.utils.timezone.now', return_value=self.NOW_UTC), \
                mock.patch('clients.tasks._send_subscription_warning') as warn:
            orchestrate_billing_and_suspensions()
        return warn

    def test_trial_that_ended_yesterday_is_suspended_on_time(self):
        t = _tenant(status='trial', trial_ends_at=self.CAIRO_TODAY - timedelta(days=1), is_active=True)
        self._run()
        t.refresh_from_db()
        self.assertEqual(t.status, 'suspended')

    def test_grace_ends_on_the_right_day(self):
        # خلص يوم 7 ⇒ السماح 8و9و10 ⇒ يتعلّق يوم 11
        t = _tenant(status='active', subscription_end_date=self.CAIRO_TODAY - timedelta(days=4), is_active=True)
        keep = _tenant(status='active', subscription_end_date=self.CAIRO_TODAY - timedelta(days=3), is_active=True)
        self._run()
        t.refresh_from_db()
        keep.refresh_from_db()
        self.assertEqual(t.status, 'suspended')
        self.assertEqual(keep.status, 'active')

    def test_warnings_count_days_from_cairo_today(self):
        t = _tenant(status='active', subscription_end_date=self.CAIRO_TODAY + timedelta(days=3), is_active=True)
        warn = self._run()
        calls = [c for c in warn.call_args_list if c.args[0].pk == t.pk]
        self.assertEqual([c.kwargs['days_left'] for c in calls], [3])


class RenewalEmailWordingTests(TestCase):
    """الإيميل كان بيوعد بـ «وضع القراءة فقط» — مش موجود: ٣ أيام سماح شغّالة عادي وبعدها بيقف."""

    def setUp(self):
        patcher = mock.patch.object(Tenant, 'auto_create_schema', False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _body(self, **kw):
        from django.core import mail
        from django.test import override_settings
        from clients.tasks import _send_subscription_warning
        t = _tenant(email=f'{uuid.uuid4().hex[:6]}@x.com', phone='', **kw)
        with override_settings(EMAIL_HOST='smtp.test', EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend'):
            _send_subscription_warning(t, days_left=1, is_trial=kw.get('status') == 'trial')
        return mail.outbox[-1].body

    def test_paid_subscription_mentions_grace_not_read_only(self):
        body = self._body(status='active')
        self.assertIn('3 أيام سماح', body)
        self.assertNotIn('القراءة فقط', body)

    def test_trial_has_no_grace(self):
        body = self._body(status='trial')
        self.assertNotIn('سماح', body)
        self.assertIn('بياناتك بتفضل محفوظة', body)
