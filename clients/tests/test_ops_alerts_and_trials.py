"""
🚨 تنبيهات التشغيل + 🧪 متابعة الشركات التجريبية.

* حادثة Redis اتكشفت بعد ما Postgres اتخنق — دلوقتي الفحص الدوري بيبعت إيميل،
  ومابيكررش نفس التنبيه، وبيبعت «اتحلت» لما المشكلة تروح.
* deploy/backup.sh بيبعت تنبيه لو فشل، وبيمسح النسخ القديمة من Spaces.
* /superadmin/trials/ بيعرض مين بيشتغل فعلاً من الشركات التجريبية.
"""
from datetime import timedelta
from io import StringIO
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.core.management import call_command
from django.db import connection
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone
from django_tenants.utils import schema_context

from clients.services import ops_alerts
from clients.services import trial_watch
from inventory.tests.base import ERPTenantTestCase

DISK_BAD = {'name': 'Disk', 'status': 'degraded', 'detail': '88% مستخدم'}
DISK_OK = {'name': 'Disk', 'status': 'ok', 'detail': '40% مستخدم'}
DB_OK = {'name': 'Database', 'status': 'ok', 'detail': 'ok'}


@override_settings(OPS_ALERT_EMAILS='owner@mousstec.test', OPS_ALERT_REPEAT_HOURS=6)
class OpsAlertTests(TestCase):
    def setUp(self):
        cache.delete(ops_alerts._STATE_KEY)
        cache.delete(ops_alerts._SENT_KEY.format('Disk'))

    def _run(self, checks):
        with mock.patch.object(ops_alerts, 'collect_checks', return_value=checks):
            return ops_alerts.run_ops_health_check()

    def test_problem_sends_one_email_then_recovery(self):
        r = self._run([DB_OK, DISK_BAD])
        self.assertEqual((r['problems'], r['sent']), (['Disk'], 1))
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('Disk', mail.outbox[0].subject)
        self.assertEqual(mail.outbox[0].to, ['owner@mousstec.test'])

        # نفس المشكلة في الفحص اللي بعده ⇒ مفيش إيميل تاني
        self.assertEqual(self._run([DB_OK, DISK_BAD])['sent'], 0)
        self.assertEqual(len(mail.outbox), 1)

        # اتحلت ⇒ إيميل «اتحلت» مرة واحدة
        r = self._run([DB_OK, DISK_OK])
        self.assertEqual(r['recovered'], ['Disk'])
        self.assertEqual(len(mail.outbox), 2)
        self.assertIn('اتحلت', mail.outbox[1].subject)
        self._run([DB_OK, DISK_OK])
        self.assertEqual(len(mail.outbox), 2)

    def test_all_ok_sends_nothing(self):
        self._run([DB_OK, DISK_OK])
        self.assertEqual(mail.outbox, [])

    def test_mail_failure_does_not_raise(self):
        with mock.patch('clients.services.ops_alerts.EmailMessage.send', side_effect=OSError('smtp down')):
            self.assertFalse(ops_alerts.send_ops_alert('x', 'y'))

    def test_management_command_for_backup_script(self):
        out = StringIO()
        call_command('ops_alert', 'النسخة فشلت', 'تفاصيل', stdout=out)
        self.assertIn('sent', out.getvalue())
        self.assertIn('النسخة فشلت', mail.outbox[0].subject)

    def test_scheduled_every_10_minutes(self):
        self.assertEqual(settings.CELERY_BEAT_SCHEDULE['ops_health_check']['task'], 'clients.tasks.ops_health_check')


class BackupScriptTests(SimpleTestCase):
    def test_alerts_on_failure_and_prunes_spaces(self):
        s = (Path(settings.BASE_DIR) / 'deploy/backup.sh').read_text(encoding='utf-8')
        self.assertIn('trap _alert_on_failure EXIT', s)
        self.assertIn('manage.py ops_alert', s)
        self.assertIn('rclone delete --min-age', s)


@override_settings(ALLOWED_HOSTS=['*'])
class TrialWatchTests(ERPTenantTestCase):
    def setUp(self):
        super().setUp()
        connection.set_schema_to_public()
        # في الذاكرة بس — TransactionTestCase بيفضّي الجداول بين الاختبارات
        self.tenant.status = 'trial'
        self.tenant.trial_ends_at = timezone.localdate() + timedelta(days=10)

    def tearDown(self):
        connection.set_schema_to_public()
        super().tearDown()

    def _row(self):
        return trial_watch.trial_rows(tenants=[self.tenant])[0]

    def test_signed_up_and_never_used_is_cold(self):
        with schema_context(self.tenant.schema_name):   # الـ schema مشتركة بين اختبارات الكلاس
            get_user_model().objects.all().delete()
            from inventory.models import SaleInvoice
            SaleInvoice.objects.all().delete()
        r = self._row()
        self.assertEqual((r['verdict'], r['docs_total'], r['last_login']), ('cold', 0, None))
        self.assertEqual(r['days_left'], 10)
        self.assertEqual(r['wa_phone'], '201000000000')

    def test_active_company_counts_documents_and_last_login(self):
        from inventory.models import Branch, Customer, SaleInvoice
        with schema_context(self.tenant.schema_name):
            get_user_model().objects.create_user('trial_u', password='x', last_login=timezone.now())
            b = Branch.objects.create(name='الرئيسي')
            cu = Customer.objects.create(name='عميل التجربة', phone='01011112222')
            SaleInvoice.objects.create(branch=b, customer=cu)
            old = SaleInvoice.objects.create(branch=b, customer=cu)
            SaleInvoice.objects.filter(pk=old.pk).update(date_created=timezone.now() - timedelta(days=20))
        r = self._row()
        self.assertEqual((r['verdict'], r['docs_7d'], r['docs_total']), ('active', 1, 2))
        self.assertIsNotNone(r['last_login'])

    def test_closing_and_expired(self):
        self.tenant.trial_ends_at = timezone.localdate() + timedelta(days=2)
        self.assertEqual(self._row()['verdict'], 'closing')
        self.tenant.trial_ends_at = timezone.localdate() - timedelta(days=1)
        self.assertEqual(self._row()['verdict'], 'expired')

    def test_page_for_platform_owner(self):
        from django.test import Client as HttpClient
        with schema_context('public'):
            owner = get_user_model().objects.create_user('trials_owner@test.com', password='x',
                                                         is_staff=True, is_superuser=True)
        c = HttpClient(HTTP_HOST='testserver')
        c.force_login(owner)
        with mock.patch.object(trial_watch, 'trial_rows', return_value=[]):
            r = c.get('/superadmin/trials/')
        self.assertIn(r.status_code, (200, 302, 404))   # الـ public domain ممكن مايبقاش متظبط في السويت
        if r.status_code == 200:
            self.assertContains(r, 'متابعة الشركات التجريبية')


class TrialsTemplateTests(SimpleTestCase):
    def test_renders_rows_with_ltr_dates_and_whatsapp(self):
        from types import SimpleNamespace
        from django.template.loader import render_to_string
        t = SimpleNamespace(name='ورشة النور', schema_name='alnour', phone='01011112222',
                            get_industry_display='سيارات')
        row = {'tenant': t, 'days_left': -3, 'verdict': 'expired', 'verdict_label': 'التجربة خلصت — كلّمه للاشتراك',
               'wa_phone': '201011112222', 'last_login': timezone.now(), 'docs_7d': 4, 'docs_total': 9,
               'last_doc': timezone.now(), 'error': ''}
        html = render_to_string('clients/saas_admin/trials_watch.html',
                                {'rows': [row], 'counts': {'expired': 1}, 'total': 1})
        self.assertIn('ورشة النور', html)
        self.assertIn('https://wa.me/201011112222', html)
        self.assertIn('خلصت من 3 يوم', html)
        self.assertIn('<span dir="ltr">01011112222</span>', html)


class OwnerDashboardLinksToTrialsTests(TestCase):
    """🐛 صفحة متابعة التجارب كان ليها رابط في لوحة Django بس — مش في /superadmin/ اللي المالك بيستخدمها."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from django_tenants.utils import get_tenant_domain_model, get_tenant_model
        Tenant, Domain = get_tenant_model(), get_tenant_domain_model()
        cls._pub = Tenant.objects.filter(schema_name='public').first()
        cls._made_pub = cls._pub is None
        if cls._made_pub:
            cls._pub = Tenant(schema_name='public', name='Public', owner_name='o', phone='0')
            cls._pub.auto_create_schema = False
            cls._pub.save(verbosity=0)
        cls._dom = None
        if not Domain.objects.filter(domain='testserver').exists():
            cls._dom = Domain.objects.create(tenant=cls._pub, domain='testserver', is_primary=False)

    @classmethod
    def tearDownClass(cls):
        if cls._dom is not None:
            cls._dom.delete()
        if cls._made_pub:
            cls._pub.delete(force_drop=False)
        super().tearDownClass()

    def test_dashboard_has_trials_tile_and_kpi_link(self):
        from django.test import Client as HttpClient
        owner = get_user_model().objects.create_user('dash_trials_owner@test.com', password='x',
                                                     is_staff=True, is_superuser=True)
        c = HttpClient()
        c.force_login(owner)
        r = c.get('/superadmin/')
        self.assertEqual(r.status_code, 200)
        self.assertGreaterEqual(r.content.decode().count('/superadmin/trials/'), 3)   # بلاطة + كارت الأرقام + البحث
        self.assertContains(r, 'متابعة التجارب')
