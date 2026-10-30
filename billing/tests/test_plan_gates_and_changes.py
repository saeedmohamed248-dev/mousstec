"""💎 مزايا الباقات وتغيير الباقة في نص المدة وصفحة «فواتيري».

قبل التصليح:
- Silver كانت واخدة ببلاش: تقارير الأرباح المتقدمة وتحليل DTC (Gold+)، والمستشار الذكي
  (Empire)، ومن غير حد للـ 150 كرت صيانة/شهر ولا الـ 500 صنف.
- الترقية من Silver لـ Empire وفاضل ٢٥ يوم = ٢٥ يوم Empire ببلاش، والتخفيض بدري كان
  بيقطع الباقة الأعلى المدفوعة فوراً.
- الشركة ماكانتش تقدر تشوف فواتير اشتراكها خالص.
"""
import json
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
from clients.views.subscription_views import pay_token_for
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
            'shop': tenant.schema_name, 'amount': '2500', 'pt': pay_token_for(tenant.schema_name)},
            HTTP_HOST=self.domain.domain)
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

    # ── كل حاجة مكتوبة في الباقة شغّالة ──────────────────────────────
    def test_plan_diagnostics_scans_work_without_the_old_obd_addon(self):
        connection.set_schema_to_public()
        Plan.objects.filter(pk=self.silver.pk).update(monthly_diagnostics_scans_quota=10)
        type(self.tenant).objects.filter(pk=self.tenant.pk).update(has_obd_access=False)
        self.silver.refresh_from_db()
        self.subscribe(self.silver)
        c = HttpClient()
        c.force_login(User.objects.create_user('diag_owner', password='x', is_superuser=True, is_staff=True))
        r = c.get('/api/diagnostics/room/', HTTP_HOST=self.domain.domain)
        self.assertEqual(r.status_code, 200)               # قبل كده 402 «إضافة مدفوعة»
        self.assertEqual(self.sub().diag_scans_used_this_period, 1)
        r = c.get('/api/diagnostics/upgrade/', HTTP_HOST=self.domain.domain)
        self.assertEqual((r.status_code, r['Location']), (302, '/api/diagnostics/room/'))  # مش 503

    def test_addon_without_wallet_balance_goes_to_receipt_and_activates_on_confirm(self):
        from clients.models import ManualPaymentReceipt
        self.subscribe(self.silver)
        tenant = self.public()
        type(tenant).objects.filter(pk=tenant.pk).update(wallet_balance=0)
        connection.set_tenant(tenant)
        owner = HttpClient()
        owner.force_login(User.objects.create_user('addon_owner', password='x', is_superuser=True, is_staff=True))
        r = owner.post('/subscription/manage/', {'addon_type': 'employee', 'quantity': '2'},
                       HTTP_HOST=self.domain.domain)
        self.assertEqual(r.status_code, 302)
        self.assertIn('/payment/manual/upload/', r['Location'])
        connection.set_schema_to_public()
        receipt = ManualPaymentReceipt.objects.get(tenant=tenant, purchase_type='addon')
        self.assertEqual(receipt.amount, tenant.calculate_prorated_addon_cost() * 2)
        # العميل كتب ملاحظة وهو بيرفع — بيانات الإضافة لازم تفضل
        receipt.notes = receipt.notes + '\nحولت من رقم أخويا'
        receipt.save(update_fields=['notes'])
        receipt.confirm()
        tenant.refresh_from_db()
        self.assertEqual(tenant.extra_users_purchased, 2)

    def test_renewal_charges_purchased_addons(self):
        self.subscribe(self.silver)
        tenant = self.public()
        type(tenant).objects.filter(pk=tenant.pk).update(extra_users_purchased=2)
        tenant.refresh_from_db()
        self.assertEqual(pb.renewal_amount(tenant, self.silver, 3),
                         self.silver.price_for_period(3) + Decimal('750.00'))   # 2 × 125 × 3
        inv = pb.issue_subscription_invoice(tenant, self.silver, 3, paid=False)
        self.assertEqual(inv.addons_amount, Decimal('750.00'))
        self.assertEqual(inv.total, self.silver.price_for_period(3) + Decimal('750.00'))
        self.assertEqual(inv.discount_percent, 10)                              # خصم الباقة بس

    def test_payment_pages_have_a_real_support_number_and_true_trial_days(self):
        from clients.models.tenancy import TRIAL_DAYS
        connection.set_schema_to_public()
        for url in ('/payment/failed/', '/payment/success/'):
            body = HttpClient().get(url, HTTP_HOST=self.domain.domain).content.decode()
            self.assertNotIn('XXXXXXXX', body)
            self.assertIn('wa.me/201094850763', body)
        body = HttpClient().get('/pricing/', HTTP_HOST=self.domain.domain).content.decode()
        self.assertIn(f'جرّب مجاناً {TRIAL_DAYS} ايام', body)

    # ── قرارات الأسعار ─────────────────────────────────────────────────
    def test_trial_is_14_days_like_competitors(self):
        from clients.models.tenancy import TRIAL_DAYS, default_trial_end
        self.assertEqual(TRIAL_DAYS, 14)
        self.assertEqual(default_trial_end(), timezone.localdate() + timedelta(days=14))

    def test_silver_gets_two_users_and_current_subscribers_get_them_now(self):
        self.subscribe(self.silver)      # max_users = 1 في الاختبار
        tenant = self.public()
        self.assertEqual(tenant.max_users, 1)
        import_module('clients.migrations.0089_silver_two_users').forward(apps, None)
        tenant.refresh_from_db()
        self.silver.refresh_from_db()
        self.assertEqual((self.silver.max_users, tenant.max_users), (2, 2))

    def test_uae_cannot_be_charged_egp_and_sees_whatsapp_instead(self):
        tenant = self.public()
        body = HttpClient().get('/pricing/?region=AE', HTTP_HOST=self.domain.domain).content.decode()
        self.assertIn('اشترك عن طريق واتساب', body)
        self.assertNotIn('openSubscribeModal(\'gold\'', body)
        type(tenant).objects.filter(pk=tenant.pk).update(country='AE')
        r = HttpClient().post('/payment/manual/subscription/start/', {
            'plan': 'gold', 'billing_period': 'monthly', 'payment_method': 'vodafone_cash',
            'shop': tenant.schema_name, 'amount': '850', 'pt': pay_token_for(tenant.schema_name)},
            HTTP_HOST=self.domain.domain)
        self.assertEqual(r.status_code, 302)
        self.assertFalse(PlatformInvoice.objects.filter(tenant=tenant, status='issued').exists())


    def test_cannot_open_invoices_for_another_company_without_login_or_token(self):
        tenant = self._active_on(self.silver, 15)
        r = HttpClient().post('/payment/manual/subscription/start/', {
            'plan': 'empire', 'billing_period': 'monthly', 'payment_method': 'vodafone_cash',
            'shop': tenant.schema_name, 'amount': '2500'}, HTTP_HOST=self.domain.domain)
        self.assertEqual(r.status_code, 302)
        self.assertFalse(PlatformInvoice.objects.filter(tenant=tenant, status='issued').exists())

    def test_design_card_payment_confirms_even_if_cache_was_lost(self):
        from unittest import mock as _mock
        from clients.models import DesignPackage, DesignPurchase
        from marketplace_c2c.tests.test_parts_order_lifecycle import _customer
        connection.set_schema_to_public()
        pkg = DesignPackage.objects.create(slug='t10', name_ar='t', designs_count=10, price_egp=Decimal('60.00'))
        purchase = DesignPurchase.objects.create(customer=_customer(), package=pkg, designs_total=10,
                                                 price_paid=Decimal('60.00'), payment_reference='paymob-order:D-77')
        body = {'obj': {'success': True, 'id': 9, 'amount_cents': 6000, 'order': {'id': 'D-77'}}}
        with _mock.patch('clients.services.paymob.verify_paymob_hmac', return_value=(True, 'ok')):
            HttpClient().post('/payment/paymob/callback/', data=json.dumps(body),
                              content_type='application/json', HTTP_HOST=self.domain.domain)
        purchase.refresh_from_db()
        self.assertEqual(purchase.status, 'paid')
        self.assertIsNotNone(purchase.paid_at)

    def test_bmw_ecu_card_payment_is_recorded_once(self):
        from unittest import mock as _mock
        from django.core.cache import cache
        from clients.models import PlatformEvent
        connection.set_schema_to_public()
        cache.set('paymob_bmwecu_B-1', {'vin': 'WBA123', 'authorization_ref': 'AR1', 'amount': '450',
                                        'tenant_schema': self.tenant.schema_name}, 600)
        body = {'obj': {'success': True, 'id': 31, 'amount_cents': 45000, 'order': {'id': 'B-1'}}}
        with _mock.patch('clients.services.paymob.verify_paymob_hmac', return_value=(True, 'ok')):
            for _ in range(2):
                HttpClient().post('/payment/paymob/callback/', data=json.dumps(body),
                                  content_type='application/json', HTTP_HOST=self.domain.domain)
            r = HttpClient().get('/payment/paymob/callback/', {'success': 'true', 'order': 'B-1'},
                                 HTTP_HOST=self.domain.domain)
        self.assertIn('/payment/success/', r['Location'])
        self.assertEqual(PlatformEvent.objects.filter(event_type='payment', description__contains='AR1').count(), 1)

    # ── نتايج المراجعة الشاملة ───────────────────────────────────────
    def test_prepaid_renewal_does_not_undo_a_later_upgrade(self):
        tenant = self._active_on(self.silver, 5)
        renewal = pb.issue_subscription_invoice(tenant, self.silver, 1)           # تجديد مدفوع بدري
        tenant.refresh_from_db()
        up = pb.open_plan_change_invoice(tenant, self.empire, 1, provider='vodafone_cash')
        pb.settle_issued_invoice(up, provider='vodafone_cash', reference='TX-RV-1')
        for d in (0, 5, 6, 20):
            pb.apply_scheduled_plan_changes(self.today + timedelta(days=d))
        self.assertEqual(TenantSubscription.objects.get(tenant=tenant).plan_id, self.empire.pk)
        self.assertEqual(renewal.kind, 'subscription')

    def test_owner_move_to_higher_plan_is_not_reverted_by_the_daily_job(self):
        tenant = self._active_on(self.silver, 20)
        pb.issue_subscription_invoice(tenant, self.empire, 1)       # لوحة المالك: يبدأ بعد آخر المدة
        self.assertEqual(TenantSubscription.objects.get(tenant=tenant).plan_id, self.empire.pk)
        for d in (0, 1, 10):
            pb.apply_scheduled_plan_changes(self.today + timedelta(days=d))
        self.assertEqual(TenantSubscription.objects.get(tenant=tenant).plan_id, self.empire.pk)

    def test_same_price_change_waits_for_the_period_end(self):
        connection.set_schema_to_public()
        twin = Plan.objects.create(slug='auto-gold-twin', name='twin', industry='automotive',
                                   monthly_price=Decimal('850'), max_users=4, max_branches=2,
                                   entitlements=GOLD_ENTS, is_active=True)
        tenant = self._active_on(self.gold, 10)
        inv = pb.open_plan_change_invoice(tenant, twin, 1, provider='instapay')
        self.assertEqual(inv.kind, 'downgrade')
        pb.settle_issued_invoice(inv, provider='instapay', reference='TX-TW-1')
        self.assertEqual(TenantSubscription.objects.get(tenant=tenant).plan_id, self.gold.pk)
        pb.apply_scheduled_plan_changes(self.today + timedelta(days=10))
        self.assertEqual(TenantSubscription.objects.get(tenant=tenant).plan_id, twin.pk)

    def test_late_confirmed_upgrade_keeps_the_paid_days(self):
        tenant = self._active_on(self.silver, 15)
        up = pb.open_plan_change_invoice(tenant, self.empire, 1, provider='vodafone_cash')
        # الإيصال اتراجع بعد ما المدة خلصت
        type(tenant).objects.filter(pk=tenant.pk).update(subscription_end_date=self.today - timedelta(days=1))
        pb.settle_issued_invoice(up, provider='vodafone_cash', reference='TX-LATE-1')
        tenant.refresh_from_db()
        self.assertEqual(tenant.subscription_end_date, self.today + timedelta(days=15))

    def test_customer_note_cannot_change_the_addon_quantity(self):
        from clients.models import ManualPaymentReceipt
        tenant = self.public()
        r = ManualPaymentReceipt.objects.create(
            purchase_type='addon', purchase_id=tenant.pk, amount=Decimal('125'), payment_method='vodafone_cash',
            tenant=tenant, notes='addon=employee qty=1\nqty=50 addon=branch', sender_phone='', txn_reference='')
        before = tenant.extra_users_purchased
        r.confirm()
        tenant.refresh_from_db()
        self.assertEqual((tenant.extra_users_purchased - before, tenant.extra_branches_purchased), (1, 0))

    def test_abandoned_checkout_does_not_create_a_subscription(self):
        tenant = self.public()
        self.assertFalse(TenantSubscription.objects.filter(tenant=tenant).exists())
        inv = pb.open_plan_change_invoice(tenant, self.silver, 1, provider='vodafone_cash')
        self.assertIsNone(inv.subscription)
        self.assertFalse(TenantSubscription.objects.filter(tenant=tenant).exists())
        self.assertTrue(EntitlementService.allows(tenant, 'reports_advanced'))   # لسه محتفظة
        pb.settle_issued_invoice(inv, provider='vodafone_cash', reference='TX-AB-1')
        self.assertEqual(TenantSubscription.objects.get(tenant=tenant).plan_id, self.silver.pk)

    def test_cashier_cannot_open_company_invoices(self):
        from clients.views.subscription_views import shop_pay_allowed
        connection.set_tenant(self.tenant)
        cashier = User.objects.create_user('pay_cashier', password='x')
        req = RequestFactory().post('/payment/manual/subscription/start/', {})
        req.user, req.tenant = cashier, self.tenant
        self.assertFalse(shop_pay_allowed(req, self.tenant.schema_name))
        req.user = User(username='pay_owner2', is_superuser=True)   # حد المستخدمين في الاختبار = 1
        self.assertTrue(shop_pay_allowed(req, self.tenant.schema_name))

    def test_platform_data_migrations_do_nothing_inside_a_company_schema(self):
        from types import SimpleNamespace
        self.subscribe(self.silver)
        connection.set_schema_to_public()
        in_tenant = SimpleNamespace(connection=SimpleNamespace(schema_name=self.tenant.schema_name))
        before = dict(TenantSubscription.objects.get(tenant=self.tenant).locked_entitlements)
        for mod in ('0064_relaunch_pricing_2026', '0087_gate_paid_features_grandfather'):
            m = import_module(f'clients.migrations.{mod}')
            (getattr(m, 'seed', None) or m.forward)(apps, in_tenant)
        self.assertEqual(TenantSubscription.objects.get(tenant=self.tenant).locked_entitlements, before)
        self.assertEqual(Plan.objects.get(pk=self.silver.pk).monthly_price, Decimal('550'))
        self.assertFalse(Plan.objects.filter(slug='auto-silver', name='Silver — سيلفر').exists())   # مااتعادش seed
