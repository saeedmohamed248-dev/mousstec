"""💎 مزايا الباقات وتغيير الباقة في نص المدة وصفحة «فواتيري».

قبل التصليح:
- Silver كانت واخدة ببلاش: تقارير الأرباح المتقدمة وتحليل DTC (Gold+)، والمستشار الذكي
  (Empire)، ومن غير حد للـ 150 كرت صيانة/شهر ولا الـ 500 صنف.
- الترقية من Silver لـ Empire وفاضل ٢٥ يوم = ٢٥ يوم Empire ببلاش، والتخفيض بدري كان
  بيقطع الباقة الأعلى المدفوعة فوراً.
- الشركة ماكانتش تقدر تشوف فواتير اشتراكها خالص.
"""
from datetime import timedelta
from decimal import Decimal
from importlib import import_module

from django.apps import apps
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import connection
from django.test import Client as HttpClient, RequestFactory, override_settings
from django.utils import timezone

from clients.models import Plan, PlatformInvoice, TenantSubscription
from clients.services import platform_billing as pb
from clients.services.entitlements import EntitlementService, require_feature
from inventory.models import Branch, Product, SaleInvoice
from inventory.tests.base import ERPTenantTestCase
from inventory.tests.factories import make_branch, make_customer
from tenancy.services.plan_limits import inventory_items_block, repair_cards_block

User = get_user_model()

SILVER_ENTS = {
    'core_invoicing': {'enabled': True},
    'workshop_repair_cards': {'enabled': True, 'monthly_limit': 2},
    'inventory_items': {'enabled': True, 'monthly_limit': 2},
    'reports_basic': {'enabled': True},
}
GOLD_ENTS = {
    'core_invoicing': {'enabled': True},
    'workshop_repair_cards': {'enabled': True, 'monthly_limit': 0},
    'workshop_dtc_ai': {'enabled': True},
    'reports_advanced': {'enabled': True},
}
EMPIRE_ENTS = {**GOLD_ENTS, 'ai_copilot': {'enabled': True}}


@override_settings(ALLOWED_HOSTS=['*'])
class PlanGatesAndChangesTests(ERPTenantTestCase):

    def setUp(self):
        super().setUp()
        connection.set_schema_to_public()
        mk = lambda slug, price, ents, users, branches: Plan.objects.update_or_create(  # noqa: E731
            slug=slug, defaults=dict(name=slug, industry='automotive', monthly_price=Decimal(price),
                                     max_users=users, max_branches=branches, max_treasuries=4,
                                     entitlements=ents, is_active=True))[0]
        self.silver = mk('auto-silver', '550', SILVER_ENTS, 1, 1)
        self.gold = mk('auto-gold', '850', GOLD_ENTS, 4, 2)
        self.empire = mk('auto-empire', '2500', EMPIRE_ENTS, 10, 5)
        self.today = timezone.localdate()
        type(self.tenant).objects.filter(pk=self.tenant.pk).update(
            industry='automotive', status='active', subscription_end_date=self.today,
            extra_users_purchased=0, extra_branches_purchased=0, extra_treasuries_purchased=0)
        self.tenant.refresh_from_db()
        TenantSubscription.objects.filter(tenant=self.tenant).delete()
        connection.set_tenant(self.tenant)

    def tearDown(self):
        connection.set_schema_to_public()
        PlatformInvoice.objects.filter(tenant=self.tenant).delete()
        TenantSubscription.objects.filter(tenant=self.tenant).delete()
        connection.set_tenant(self.tenant)
        super().tearDown()

    # ── helpers ──────────────────────────────────────────────────────
    def subscribe(self, plan, months=1):
        """اشتراك مدفوع بالطريقة العادية (فاتورة → mark_paid → snapshot من الباقة)."""
        connection.set_schema_to_public()
        tenant = type(self.tenant).objects.get(pk=self.tenant.pk)
        pb.issue_subscription_invoice(tenant, plan, months)
        self.tenant = type(self.tenant).objects.get(pk=self.tenant.pk)
        connection.set_tenant(self.tenant)

    def sub(self):
        return TenantSubscription.objects.select_related('plan').get(tenant=self.tenant)

    def public(self):
        connection.set_schema_to_public()
        self.tenant = type(self.tenant).objects.get(pk=self.tenant.pk)
        return self.tenant

    # ── قفل المزايا ──────────────────────────────────────────────────
    def test_silver_is_locked_out_of_gold_and_empire_features(self):
        self.subscribe(self.silver)
        for code in ('reports_advanced', 'workshop_dtc_ai', 'ai_copilot'):
            self.assertFalse(EntitlementService.allows(self.tenant, code), code)
        self.subscribe(self.empire)
        for code in ('reports_advanced', 'workshop_dtc_ai', 'ai_copilot'):
            self.assertTrue(EntitlementService.allows(self.tenant, code), code)

    def test_company_without_subscription_keeps_newly_gated_features(self):
        self.assertTrue(EntitlementService.allows(self.tenant, 'reports_advanced'))
        self.assertIsNone(repair_cards_block(self.tenant))

    def test_locked_page_names_the_feature_and_the_plan_that_has_it(self):
        self.subscribe(self.silver)

        @require_feature('reports_advanced', allow_without_subscription=True)
        def view(_request):
            raise AssertionError('should be locked')

        resp = view(RequestFactory().get('/system/reports/pnl/'))
        self.assertEqual(resp.status_code, 403)
        body = resp.content.decode()
        self.assertIn('auto-gold', body)          # أرخص باقة فيها الميزة
        self.assertIn('/subscription/manage/', body)

    def test_dtc_and_copilot_endpoint_respect_the_plan(self):
        self.subscribe(self.silver)
        c = HttpClient()
        c.force_login(User.objects.create_user('gate_owner', password='x', is_superuser=True, is_staff=True))
        r = c.get('/system/api/v1/ai/estimate-repair/', {'dtc': 'P0301'}, HTTP_HOST=self.domain.domain)
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()['code'], 'feature_not_in_plan')
        self.assertIn('message', r.json())        # الشات بيعرض message
        r = c.get('/system/api/v1/ai/estimate-repair/', {'query': 'مبيعات النهارده'}, HTTP_HOST=self.domain.domain)
        self.assertEqual(r.json()['feature'], 'ai_copilot')

    def test_monthly_repair_cards_limit(self):
        self.subscribe(self.silver)
        branch, customer = make_branch(), make_customer()
        for _ in range(2):
            SaleInvoice.objects.create(customer=customer, branch=branch, invoice_type='maintenance')
        self.assertIn('2', repair_cards_block(self.tenant))
        # فاتورة البيع العادية مش كرت صيانة
        self.subscribe(self.gold)
        self.assertIsNone(repair_cards_block(self.tenant))   # Gold = غير محدود

    def test_inventory_items_limit(self):
        self.subscribe(self.silver)
        Product.objects.create(part_number='LIM-1', name='a', car_model='x')
        self.assertIsNone(inventory_items_block(self.tenant))
        Product.objects.create(part_number='LIM-2', name='b', car_model='x')
        self.assertIsNotNone(inventory_items_block(self.tenant))
        self.assertIsNotNone(inventory_items_block(self.tenant, adding=1))

    def test_existing_subscriptions_are_grandfathered_until_renewal(self):
        self.subscribe(self.silver)
        connection.set_schema_to_public()
        import_module('clients.migrations.0087_gate_paid_features_grandfather').forward(apps, None)
        connection.set_tenant(self.tenant)
        self.tenant = type(self.tenant).objects.get(pk=self.tenant.pk)
        for code in ('reports_advanced', 'workshop_dtc_ai', 'ai_copilot'):
            self.assertTrue(EntitlementService.allows(self.tenant, code), code)
        self.assertIsNone(EntitlementService.limit(self.tenant, 'workshop_repair_cards') or None)
        # أول تجديد بعد كده = قواعد الباقة الحالية
        self.subscribe(self.silver)
        self.assertFalse(EntitlementService.allows(self.tenant, 'reports_advanced'))

    # ── الترقية والتخفيض ─────────────────────────────────────────────
    def _active_on(self, plan, days_left):
        self.subscribe(plan)
        tenant = self.public()
        type(tenant).objects.filter(pk=tenant.pk).update(
            subscription_end_date=self.today + timedelta(days=days_left), status='active')
        return type(tenant).objects.get(pk=tenant.pk)

    def test_upgrade_charges_only_the_remaining_days_and_switches_now(self):
        tenant = self._active_on(self.silver, 15)
        q = pb.quote_plan_change(tenant, self.empire, 12)
        self.assertEqual(q['kind'], 'upgrade')
        self.assertEqual(q['total'], Decimal('975.00'))      # (2500-550)/30 × 15
        inv = pb.open_plan_change_invoice(tenant, self.empire, 12, provider='vodafone_cash')
        self.assertEqual((inv.kind, inv.total), ('upgrade', Decimal('975.00')))
        pb.settle_issued_invoice(inv, provider='vodafone_cash', reference='TX-UP-1')
        tenant.refresh_from_db()
        sub = TenantSubscription.objects.get(tenant=tenant)
        self.assertEqual(sub.plan_id, self.empire.pk)                    # فوراً
        self.assertEqual(tenant.subscription_end_date, self.today + timedelta(days=15))  # من غير تمديد
        self.assertTrue(sub.effective_entitlements.get('ai_copilot', {}).get('enabled'))

    def test_downgrade_starts_after_the_paid_period(self):
        tenant = self._active_on(self.empire, 10)
        q = pb.quote_plan_change(tenant, self.silver, 1)
        self.assertEqual(q['kind'], 'downgrade')
        self.assertEqual(q['total'], Decimal('550.00'))
        inv = pb.open_plan_change_invoice(tenant, self.silver, 1, provider='instapay')
        pb.settle_issued_invoice(inv, provider='instapay', reference='TX-DOWN-1')
        tenant.refresh_from_db()
        self.assertEqual(TenantSubscription.objects.get(tenant=tenant).plan_id, self.empire.pk)  # لسه Empire
        self.assertEqual(tenant.subscription_end_date, self.today + timedelta(days=40))
        self.assertEqual(pb.apply_scheduled_plan_changes(self.today), 0)
        self.assertEqual(pb.apply_scheduled_plan_changes(self.today + timedelta(days=10)), 1)
        sub = TenantSubscription.objects.get(tenant=tenant)
        self.assertEqual(sub.plan_id, self.silver.pk)
        self.assertFalse(sub.effective_entitlements.get('ai_copilot'))
        tenant.refresh_from_db()
        self.assertEqual(tenant.max_users, 1)

    def test_downgrade_blocked_while_usage_exceeds_new_limits(self):
        tenant = self._active_on(self.empire, 10)
        connection.set_tenant(tenant)
        Branch.objects.create(name='B1', location='x', phone='01000000001')
        Branch.objects.create(name='B2', location='x', phone='01000000002')
        connection.set_schema_to_public()
        q = pb.quote_plan_change(tenant, self.silver, 1)
        self.assertTrue(q['blockers'])
        with self.assertRaises(ValidationError):
            pb.open_plan_change_invoice(tenant, self.silver, 1, provider='instapay')

    def test_manual_payment_start_uses_the_prorated_amount(self):
        tenant = self._active_on(self.silver, 15)
        connection.set_tenant(tenant)
        r = HttpClient().post('/payment/manual/subscription/start/', {
            'plan': 'empire', 'billing_period': 'monthly', 'payment_method': 'vodafone_cash',
            'shop': tenant.schema_name, 'amount': '2500'}, HTTP_HOST=self.domain.domain)
        self.assertEqual(r.status_code, 302)
        connection.set_schema_to_public()
        inv = PlatformInvoice.objects.filter(tenant=tenant, status='issued').latest('pk')
        self.assertEqual((inv.kind, inv.total), ('upgrade', Decimal('975.00')))

    # ── «فواتيري» ────────────────────────────────────────────────────
    def test_owner_sees_own_invoices_and_can_open_them(self):
        self.subscribe(self.gold)
        connection.set_schema_to_public()
        inv = PlatformInvoice.objects.filter(tenant=self.tenant).latest('pk')
        connection.set_tenant(self.tenant)
        owner = HttpClient()
        owner.force_login(User.objects.create_user('inv_owner', password='x', is_superuser=True, is_staff=True))
        r = owner.get('/subscription/invoices/', HTTP_HOST=self.domain.domain)
        self.assertContains(r, inv.invoice_number)
        r = owner.get(f'/subscription/invoices/{inv.invoice_number}/', HTTP_HOST=self.domain.domain)
        self.assertContains(r, inv.invoice_number)
        r = owner.get('/subscription/invoices/INV-0000-000000/', HTTP_HOST=self.domain.domain)
        self.assertIn(r.status_code, (302, 404))   # الموقع بيحوّل الـ 404 — المهم مايظهرش

    def test_staff_cannot_see_company_invoices(self):
        connection.set_tenant(self.tenant)
        staff = HttpClient()
        staff.force_login(User.objects.create_user('inv_staff', password='x'))
        r = staff.get('/subscription/invoices/', HTTP_HOST=self.domain.domain)
        self.assertEqual(r.status_code, 302)

    def test_daily_switch_does_not_undo_an_upgrade(self):
        tenant = self._active_on(self.silver, 15)
        inv = pb.open_plan_change_invoice(tenant, self.empire, 1, provider='vodafone_cash')
        pb.settle_issued_invoice(inv, provider='vodafone_cash', reference='TX-UP-2')
        sub = TenantSubscription.objects.get(tenant=tenant)
        locked_at = sub.locked_at
        for d in range(3):
            self.assertEqual(pb.apply_scheduled_plan_changes(self.today + timedelta(days=d)), 0)
        sub.refresh_from_db()
        self.assertEqual((sub.plan_id, sub.locked_at), (self.empire.pk, locked_at))
