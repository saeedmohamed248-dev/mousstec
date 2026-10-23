"""🏢 /account/workspace/ — تغيير المجال في الفترة التجريبية (مرتين بالكتير) والحذف النهائي للحساب.

🐛 اللي سجّل مطبعة وطلعله حساب قطع غيار (أو العكس) ماكانش ليه طريقة يصلّحها غير الدعم،
ومكانش في أي طريقة المالك يمسح حسابه بنفسه.
"""
import io
import uuid
import zipfile
from datetime import timedelta
from decimal import Decimal as D
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import connection
from django.test import Client as HttpClient, override_settings
from django.utils import timezone
from django_tenants.utils import schema_context, schema_exists

from clients.models import Client, Domain, EscrowLedger, Plan, PlatformEvent, TenantSubscription
from clients.services import workspace as ws
from inventory.tests.base import ERPTenantTestCase, delete_test_tenant

User = get_user_model()
URL = '/account/workspace/'


def _plans():
    auto, _ = Plan.objects.update_or_create(slug='auto-empire', defaults={
        'name': 'Empire', 'industry': 'automotive', 'monthly_price': D('2500'), 'is_active': True,
        'max_branches': 5, 'max_users': 10, 'max_treasuries': 4,
        'entitlements': {'workshop_jobs': {'enabled': True}}})
    printing, _ = Plan.objects.update_or_create(slug='print-enterprise', defaults={
        'name': 'Print Enterprise', 'industry': 'printing', 'monthly_price': D('2000'), 'is_active': True,
        'max_branches': 4, 'max_users': 6, 'max_treasuries': 6,
        'entitlements': {'print_orders': {'enabled': True}}})
    return auto, printing


@override_settings(ALLOWED_HOSTS=['*'])
class IndustryChangeTests(ERPTenantTestCase):

    def setUp(self):
        super().setUp()
        connection.set_schema_to_public()
        self.auto_plan, self.print_plan = _plans()
        Client.objects.filter(pk=self.tenant.pk).update(
            industry='automotive', business_type='service_center', plan='empire', status='trial',
            trial_ends_at=timezone.now().date() + timedelta(days=5), industry_changes=0)
        TenantSubscription.objects.filter(tenant_id=self.tenant.pk).delete()
        TenantSubscription.objects.create(tenant_id=self.tenant.pk, plan=self.auto_plan, is_active=False)
        connection.set_tenant(self.tenant)
        self.owner = User.objects.create_superuser(f'owner_{uuid.uuid4().hex[:6]}', 'owner@ws.test', 'Str0ng#Pass')
        self.c = HttpClient()
        self.c.force_login(self.owner)

    def tearDown(self):
        connection.set_tenant(self.tenant)
        super().tearDown()

    def fresh(self):
        with schema_context('public'):
            return Client.objects.get(pk=self.tenant.pk)

    def post_change(self, target):
        return self.c.post(URL, {'action': 'change_industry', 'target': target}, HTTP_HOST=self.domain.domain)

    def test_owner_switches_automotive_to_printing(self):
        r = self.post_change('printing:print_shop')
        self.assertRedirects(r, URL, fetch_redirect_response=False)
        page = self.c.get(URL, HTTP_HOST=self.domain.domain)
        self.assertContains(page, 'id="workspaceMessage"')
        self.assertContains(page, 'باقيلك 1 مرة تغيير')

        t = self.fresh()
        self.assertEqual((t.industry, t.business_type, t.plan), ('printing', 'print_shop', 'print_enterprise'))
        self.assertEqual(t.industry_changes, 1)
        self.assertEqual((t.max_branches, t.max_users), (4, 6))      # حدود الباقة الجديدة
        with schema_context('public'):
            sub = TenantSubscription.objects.get(tenant_id=t.pk)
            # الصلاحيات المثبتة من باقة المطابع — مش Empire بتاعة السيارات
            self.assertEqual(sub.plan_id, self.print_plan.pk)
            self.assertEqual(sub.locked_entitlements, {'print_orders': {'enabled': True}})

        with schema_context('public'):                 # في «سجل التدقيق» عند مالك المنصة
            event = PlatformEvent.objects.get(event_type='industry_change', tenant_schema=t.schema_name)
            self.assertEqual((event.metadata['old_industry'], event.metadata['industry']), ('automotive', 'printing'))

        from printing.models import PrintBranch, PrintTreasury
        connection.set_tenant(self.tenant)
        self.assertTrue(PrintBranch.objects.filter(name='الفرع الرئيسي').exists())
        self.assertTrue(PrintTreasury.objects.filter(name='الخزينة النقدية (الرئيسية)').exists())

    def test_only_two_changes_during_trial(self):
        ws.change_industry(self.fresh(), 'printing', 'print_shop', user=self.owner)
        ws.change_industry(self.fresh(), 'automotive', 'parts_dealer', user=self.owner)
        t = self.fresh()
        self.assertEqual((t.industry, t.business_type, t.plan), ('automotive', 'parts_dealer', 'empire'))
        self.assertEqual(t.industry_changes_left, 0)

        r = self.post_change('printing:print_shop')
        self.assertEqual(r.status_code, 400)
        self.assertContains(r, 'استخدمت مرات تغيير المجال المسموحة', status_code=400)
        self.assertEqual(self.fresh().industry, 'automotive')

    def test_no_change_after_paying_or_after_trial_ends(self):
        with schema_context('public'):
            Client.objects.filter(pk=self.tenant.pk).update(status='active')
        with self.assertRaisesMessage(ws.WorkspaceError, 'الفترة التجريبية بس'):
            ws.change_industry(self.fresh(), 'printing', 'print_shop', user=self.owner)

        with schema_context('public'):
            Client.objects.filter(pk=self.tenant.pk).update(
                status='trial', trial_ends_at=timezone.now().date() - timedelta(days=1))
        with self.assertRaisesMessage(ws.WorkspaceError, 'الفترة التجريبية خلصت'):
            ws.change_industry(self.fresh(), 'printing', 'print_shop', user=self.owner)
        self.assertEqual(self.fresh().industry_changes, 0)

    def test_business_type_must_belong_to_the_industry(self):
        r = self.post_change('printing:service_center')
        self.assertEqual(r.status_code, 400)
        t = self.fresh()
        self.assertEqual((t.industry, t.industry_changes), ('automotive', 0))

    def test_page_and_banner_for_the_owner_only(self):
        r = self.c.get(URL, HTTP_HOST=self.domain.domain)
        self.assertContains(r, 'id="industryForm"')
        self.assertContains(r, 'value="printing:print_shop"')
        self.assertNotContains(r, 'value="automotive:service_center"')   # المجال الحالي مش من الاختيارات
        self.assertContains(r, 'id="deleteForm"')

        dash = self.c.get('/system/dashboard/', HTTP_HOST=self.domain.domain)
        self.assertContains(dash, 'id="industryChangeLink"')
        admin_index = self.c.get(f'/{settings.ADMIN_URL}/', HTTP_HOST=self.domain.domain)
        self.assertContains(admin_index, 'id="industryChangeBanner"')
        self.assertContains(admin_index, f'href="{URL}"', count=2)      # البانر + قايمة المستخدم

        staff = User.objects.create_user('ws_staff', 'staff@ws.test', 'Str0ng#Pass', is_staff=True)
        c2 = HttpClient()
        c2.force_login(staff)
        self.assertEqual(c2.get(URL, HTTP_HOST=self.domain.domain).status_code, 403)
        r = c2.post(URL, {'action': 'change_industry', 'target': 'printing:print_shop'},
                    HTTP_HOST=self.domain.domain)
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.fresh().industry, 'automotive')


@override_settings(ALLOWED_HOSTS=['*'])
class DeleteWorkspaceTests(ERPTenantTestCase):
    """كل اختبار بيعمل شركة لوحده — الحذف بيمسح الـ schema."""

    def setUp(self):
        super().setUp()
        connection.set_schema_to_public()
        suffix = uuid.uuid4().hex[:6]
        self.victim = Client(schema_name=f'del_{suffix}', name='مطبعة للحذف', owner_name='المالك',
                             phone='01000000000', email='owner@del.test', industry='printing',
                             business_type='print_shop', plan='print_enterprise')
        self.victim.save(verbosity=0)
        self.victim_domain = Domain.objects.create(tenant=self.victim, domain=f'del-{suffix}.test.com',
                                                   is_primary=True)
        self.host = self.victim_domain.domain
        self.slug = f'del-{suffix}'
        with schema_context(self.victim.schema_name):
            self.owner = User.objects.create_superuser('owner@del.test', 'owner@del.test', 'Str0ng#Pass')
            self.c = HttpClient()
            self.c.force_login(self.owner)

    def tearDown(self):
        if Client.objects.filter(pk=self.victim.pk).exists():
            delete_test_tenant(self.victim, self.victim_domain)
        connection.set_tenant(self.tenant)
        super().tearDown()

    def post_delete(self, password='Str0ng#Pass', confirm=None):
        return self.c.post(URL, {'action': 'delete_workspace', 'password': password,
                                 'confirm': self.slug if confirm is None else confirm, 'understood': 'on'},
                           HTTP_HOST=self.host)

    def _order_with_file(self):
        from printing.models import PrintCustomer, PrintOrder
        with schema_context(self.victim.schema_name):
            customer = PrintCustomer.objects.create(name='عميل الحذف', phone='01055554444')
            order = PrintOrder.objects.create(customer=customer)
            order.project_file.save('brief.pdf', ContentFile(b'%PDF-1.4 test'), save=True)
            return order.project_file.name

    def test_owner_deletes_the_workspace_permanently(self):
        page = self.c.get(URL, HTTP_HOST=self.host)
        self.assertContains(page, 'id="deleteForm"')
        self.assertContains(page, f'<span dir="ltr" class="text-red-300">{self.slug}</span>')
        stored = self._order_with_file()
        self.assertTrue(default_storage.exists(stored))

        with mock.patch.object(ws, '_in_background', side_effect=lambda fn, *args: fn(*args)):
            r = self.post_delete(confirm=self.slug.upper())     # الكابيتال مايفرقش
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'id="workspaceDeleted"')
        connection.set_schema_to_public()
        self.assertFalse(Client.objects.filter(pk=self.victim.pk).exists())
        self.assertFalse(Domain.objects.filter(domain=self.host).exists())
        self.assertFalse(TenantSubscription.objects.filter(tenant_id=self.victim.pk).exists())
        self.assertFalse(schema_exists(self.victim.schema_name))
        self.assertNotIn('_auth_user_id', self.c.session)
        # الملفات المرفوعة اتمسحت من التخزين
        self.assertFalse(default_storage.exists(stored))
        # «سجل التدقيق» عند مالك المنصة + إيميل للعميل
        event = PlatformEvent.objects.get(event_type='tenant_deleted', tenant_schema=self.victim.schema_name)
        self.assertEqual((event.metadata['email'], event.metadata['files']), ('owner@del.test', 1))
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ['owner@del.test'])
        self.assertIn('مطبعة للحذف', mail.outbox[0].subject)

    def test_owner_downloads_a_copy_of_the_data(self):
        self._order_with_file()
        r = self.c.get('/account/workspace/export/', HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r['Content-Type'], 'application/zip')
        self.assertIn(f'{self.slug}-mousstec-', r['Content-Disposition'])
        zf = zipfile.ZipFile(io.BytesIO(b''.join(r.streaming_content)))
        customers = zf.read('printing/printcustomer.csv').decode('utf-8-sig')
        self.assertIn('عميل الحذف', customers)
        users = zf.read('auth/user.csv').decode('utf-8-sig')
        self.assertIn('owner@del.test', users)
        self.assertNotIn('password', users.splitlines()[0])        # من غير كلمات السر
        with schema_context(self.victim.schema_name):
            staff = User.objects.create_user('ws_staff2', 'staff2@del.test', 'Str0ng#Pass', is_staff=True)
            c2 = HttpClient()
            c2.force_login(staff)
        self.assertEqual(c2.get('/account/workspace/export/', HTTP_HOST=self.host).status_code, 403)

    def test_wrong_password_or_confirmation_deletes_nothing(self):
        r = self.post_delete(password='wrong')
        self.assertContains(r, 'كلمة السر مش صحيحة', status_code=400)
        r = self.post_delete(confirm='something-else')
        self.assertContains(r, 'اكتب اسم الرابط زي ما هو', status_code=400)
        connection.set_schema_to_public()
        self.assertTrue(Client.objects.filter(pk=self.victim.pk).exists())
        self.assertTrue(schema_exists(self.victim.schema_name))

    def test_money_or_financial_records_block_the_deletion(self):
        connection.set_schema_to_public()
        Client.objects.filter(pk=self.victim.pk).update(wallet_balance=D('150'))
        page = self.c.get(URL, HTTP_HOST=self.host)
        self.assertContains(page, 'id="deleteBlockers"')
        self.assertNotContains(page, 'id="deleteForm"')
        r = self.post_delete()
        self.assertContains(r, 'في محفظتك 150', status_code=400)

        connection.set_schema_to_public()
        Client.objects.filter(pk=self.victim.pk).update(wallet_balance=0)
        EscrowLedger.objects.create(client_id=self.victim.pk, transaction_type='deposit', amount=D('150'),
                                    description='شحن')
        # الرصيد اتصرف — بس حركة المحفظة سجل مالي لازم يفضل، فالحذف لسه ممنوع
        Client.objects.filter(pk=self.victim.pk).update(wallet_balance=0)
        blockers = ws.deletion_blockers(Client.objects.get(pk=self.victim.pk))
        self.assertEqual(len(blockers), 1)
        self.assertIn(str(EscrowLedger._meta.verbose_name_plural), blockers[0])
        r = self.post_delete()
        self.assertEqual(r.status_code, 400)
        connection.set_schema_to_public()
        self.assertTrue(Client.objects.filter(pk=self.victim.pk).exists())
        self.assertTrue(schema_exists(self.victim.schema_name))
