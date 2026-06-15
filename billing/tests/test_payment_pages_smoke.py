"""
Smoke tests for every payment-facing page.

Each test renders the template (or hits the view through RequestFactory) with
a realistic but minimal context. The goal is to catch the class of bugs we
just hit in production:

  * `{% load humanize %}` of unregistered tag libraries
  * Templates that read context variables a view does not always pass
  * Hard-coded button URLs that point at the wrong flow
  * Missing imports / `NameError` on a code path triggered by a specific
    tenant state (no subscription, no shop, no plan row, etc.)

These are deliberately *cheap* — no full end-to-end click-through, no
external network calls. The point is to lock the templates so we can refactor
without re-discovering the same humanize-style bug six months later.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.contrib.messages.storage.cookie import CookieStorage
from django.test import RequestFactory, TestCase
from django.template.loader import render_to_string

from clients.models import (
    Client as TenantClient,
    DesignPackage,
    DesignPurchase,
    DiagnosticsTopUpPack,
    ManualPaymentReceipt,
    MarketplaceCustomer,
    Plan,
    PlanRevision,
    PlatformInvoice,
    TenantSubscription,
)


User = get_user_model()
RF = RequestFactory()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
def _bare_tenant():
    """Tenant row WITHOUT triggering django-tenants schema provisioning."""
    schema = f'tst_{uuid.uuid4().hex[:8]}'
    TenantClient.objects.bulk_create([TenantClient(
        schema_name=schema,
        name='ورشة الاختبار',
        owner_name='عبدالله',
        phone='01000000000',
        industry='automotive',
        business_type='service_center',
    )])
    return TenantClient.objects.get(schema_name=schema)


def _customer():
    c = MarketplaceCustomer.objects.create(
        customer_type='individual',
        full_name='علي',
        phone=f'+2010{uuid.uuid4().int % 100000000:08d}',
        sector='printing',
        is_verified=True,
    )
    c.session_token = uuid.uuid4()
    c.save(update_fields=['session_token'])
    return c


def _with_messages(request):
    """Some views call django.contrib.messages.add — wire cookie storage
    (doesn't require session middleware to be active in tests)."""
    setattr(request, 'session', {})
    setattr(request, '_messages', CookieStorage(request))


# ===========================================================================
# 1. /pricing/  — plans landing
# ===========================================================================
class PricingPageSmokeTests(TestCase):
    """Public pricing page renders for: no tenant, valid tenant, missing
    Plan rows. The view now degrades to an empty catalog instead of 500ing."""

    def _call(self, **get):
        req = RF.get('/pricing/', data=get)
        req.user = AnonymousUser()
        _with_messages(req)
        from clients.views.subscription_views import saas_pricing_page
        return saas_pricing_page(req)

    def test_anonymous_no_shop_renders_200(self):
        resp = self._call()
        self.assertEqual(resp.status_code, 200)
        # Must include both manual payment buttons (Vodafone + InstaPay)
        self.assertIn(b'vodafone_cash', resp.content)
        self.assertIn(b'payWithInstapay', resp.content)

    def test_with_unknown_shop_param_renders_200(self):
        resp = self._call(shop='does-not-exist')
        self.assertEqual(resp.status_code, 200)

    def test_with_valid_tenant_renders_200(self):
        t = _bare_tenant()
        resp = self._call(shop=t.schema_name)
        self.assertEqual(resp.status_code, 200)
        self.assertIn(t.name.encode('utf-8'), resp.content)

    def test_with_no_plans_active_still_renders(self):
        # Defensive path: deactivate every Plan row.
        Plan.objects.update(is_active=False)
        try:
            resp = self._call()
            self.assertEqual(resp.status_code, 200)
        finally:
            Plan.objects.update(is_active=True)


# ===========================================================================
# 2. /payment/manual/upload/<receipt_code>/  — unified upload page
# ===========================================================================
class ManualUploadPageSmokeTests(TestCase):
    """All purchase_types must render the upload page with both handles."""

    def setUp(self):
        self.customer = _customer()
        self.pkg = DesignPackage.objects.create(
            slug=f'smk-{uuid.uuid4().hex[:6]}',
            target_audience='customer',
            name_ar='باقة دخان',
            designs_count=10,
            price_egp=Decimal('250.00'),
            is_active=True,
        )

    def _render_for(self, purchase_type, purchase_id, **extra):
        receipt = ManualPaymentReceipt.objects.create(
            purchase_type=purchase_type,
            purchase_id=purchase_id,
            amount=Decimal('150.00'),
            payment_method='vodafone_cash',
            customer=self.customer,
            sender_phone='', txn_reference='',
            **extra,
        )
        from clients.views.manual_payment_views import manual_payment_upload
        req = RF.get(f'/payment/manual/upload/{receipt.receipt_code}/')
        return manual_payment_upload(req, receipt_code=receipt.receipt_code)

    def test_design_receipt(self):
        purchase = DesignPurchase.objects.create(
            customer=self.customer, package=self.pkg,
            designs_total=10, price_paid=self.pkg.price_egp,
            payment_method='vodafone_cash', status='pending',
        )
        resp = self._render_for('design', purchase.pk)
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode('utf-8')
        self.assertIn('01094850763', body)
        self.assertIn('@instapay', body)

    def test_unknown_purchase_type_does_not_crash(self):
        # Even a dangling receipt (no underlying purchase) should render.
        resp = self._render_for('design', 999_999_999)
        self.assertEqual(resp.status_code, 200)


# ===========================================================================
# 3. /subscription/topup/diagnostics/  — tenant top-up
# ===========================================================================
class DiagTopupPageSmokeTests(TestCase):
    """Page must render for tenants WITHOUT an active subscription."""

    def setUp(self):
        self.tenant = _bare_tenant()
        self.user = User.objects.create_user(
            username=f'op_{uuid.uuid4().hex[:6]}',
            password='x', is_staff=True,
        )

    def test_renders_for_tenant_without_subscription(self):
        from clients.views.manual_payment_views import diag_topup_purchase
        req = RF.get('/subscription/topup/diagnostics/')
        req.tenant = self.tenant
        req.user = self.user
        # diag_topup_purchase is @login_required; bypass the decorator by
        # calling the wrapped view directly.
        resp = diag_topup_purchase.__wrapped__(req)
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode('utf-8')
        # 🔎 The "humanize" bug was: `{% load humanize %}` of an
        # unregistered library → TemplateSyntaxError → 500. If the page
        # renders at all here, that whole class of bug is gone.
        self.assertNotIn('intcomma', body)
        self.assertIn('01094850763', body)
        self.assertIn('@instapay', body)

    def test_renders_when_no_active_packs(self):
        from clients.views.manual_payment_views import diag_topup_purchase
        DiagnosticsTopUpPack.objects.update(is_active=False)
        try:
            req = RF.get('/subscription/topup/diagnostics/')
            req.tenant = self.tenant
            req.user = self.user
            resp = diag_topup_purchase.__wrapped__(req)
            self.assertEqual(resp.status_code, 200)
            body = resp.content.decode('utf-8')
            self.assertIn('لا توجد حزم شحن متاحة', body)
        finally:
            DiagnosticsTopUpPack.objects.filter(slug='diag-30').update(is_active=True)


# ===========================================================================
# 4. Smart Diagnostics upgrade page
# ===========================================================================
class DiagnosticsUpgradePageSmokeTests(TestCase):
    """Template must render in every branch (already-premium, scan-quota gate,
    obd-addon gate, normal upgrade) WITHOUT showing an empty price."""

    def setUp(self):
        self.plan = Plan.objects.filter(slug='premium_diagnostics').first()

    def _render(self, **ctx):
        ctx.setdefault('features', [])
        ctx.setdefault('shop', 'test-shop')
        return render_to_string('smart_diagnostics/upgrade.html', ctx)

    def test_normal_upgrade_with_plan_shows_price(self):
        html = self._render(plan=self.plan)
        # Plan price must appear, not just "ج.م".
        self.assertIn(str(int(self.plan.monthly_price)), html)
        # Vodafone & InstaPay forms point at the unified manual-pay endpoint.
        self.assertIn('/payment/manual/subscription/start/', html)
        self.assertIn('value="vodafone_cash"', html)
        self.assertIn('value="instapay"', html)
        # 🔥 Regression guard: the OLD bug pointed Vodafone Cash at the
        # CUSTOMER-facing diagnostics marketplace. Make sure that link
        # is gone — the tenant must NOT be sent there from the upgrade.
        self.assertNotIn('href="/marketplace/diagnostics/"', html)

    def test_scan_quota_gate_shows_topup_cta(self):
        html = self._render(
            plan=self.plan, gate='scan_quota',
            topup_url='/subscription/topup/diagnostics/',
            reason='انتهت الحصة', plan_limit=10,
        )
        self.assertIn('/subscription/topup/diagnostics/', html)
        self.assertIn('انتهت الحصة', html)

    def test_already_premium_shows_dashboard_link(self):
        html = self._render(plan=self.plan, already_premium=True)
        self.assertIn('/system/dashboard/', html)
        self.assertNotIn('/payment/manual/subscription/start/', html)

    def test_missing_plan_does_not_crash(self):
        # The Premium Diagnostics row could be missing mid-deploy.
        html = self._render(plan=None, reason='غير متاحة')
        self.assertIn('غير متاحة', html)


# ===========================================================================
# 5. Manual subscription start — validation paths
# ===========================================================================
class ManualPaySubscriptionStartTests(TestCase):
    """The endpoint that the Vodafone/InstaPay buttons POST to."""

    def setUp(self):
        self.tenant = _bare_tenant()

    def _post(self, **data):
        from clients.views.manual_payment_views import manual_pay_subscription_start
        req = RF.post('/payment/manual/subscription/start/', data=data)
        req.user = AnonymousUser()
        _with_messages(req)
        return manual_pay_subscription_start(req)

    def test_get_redirects_to_pricing(self):
        from clients.views.manual_payment_views import manual_pay_subscription_start
        req = RF.get('/payment/manual/subscription/start/')
        req.user = AnonymousUser()
        _with_messages(req)
        resp = manual_pay_subscription_start(req)
        self.assertIn(resp.status_code, (301, 302))

    def test_bad_payment_method_redirects(self):
        resp = self._post(payment_method='bitcoin', plan='silver',
                          shop=self.tenant.schema_name, amount='550')
        self.assertIn(resp.status_code, (301, 302))

    def test_unknown_shop_redirects(self):
        resp = self._post(payment_method='vodafone_cash', plan='silver',
                          shop='not-real', amount='550')
        self.assertIn(resp.status_code, (301, 302))

    def test_valid_subscription_creates_receipt_and_redirects_to_upload(self):
        plan = Plan.objects.filter(slug='auto-silver', is_active=True).first()
        if plan is None:
            self.skipTest('seed plans not present')
        resp = self._post(
            payment_method='vodafone_cash',
            plan='silver',  # legacy slug → resolved to auto-silver
            shop=self.tenant.schema_name,
            amount=str(plan.monthly_price),
            billing_period='monthly',
        )
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/payment/manual/upload/', resp.url)
        # Receipt + invoice exist.
        receipt = ManualPaymentReceipt.objects.filter(tenant=self.tenant).first()
        self.assertIsNotNone(receipt)
        self.assertEqual(receipt.purchase_type, 'subscription')
        self.assertEqual(receipt.payment_method, 'vodafone_cash')
        invoice = PlatformInvoice.objects.filter(pk=receipt.purchase_id).first()
        self.assertIsNotNone(invoice)
        self.assertEqual(invoice.status, 'issued')

    def test_instapay_method_creates_receipt_with_correct_method(self):
        plan = Plan.objects.filter(slug='auto-silver', is_active=True).first()
        if plan is None:
            self.skipTest('seed plans not present')
        resp = self._post(
            payment_method='instapay',
            plan='silver', shop=self.tenant.schema_name,
            amount=str(plan.monthly_price), billing_period='monthly',
        )
        self.assertEqual(resp.status_code, 302)
        r = ManualPaymentReceipt.objects.filter(tenant=self.tenant).latest('created_at')
        self.assertEqual(r.payment_method, 'instapay')


# ===========================================================================
# 6. Payment result pages
# ===========================================================================
class PaymentResultPagesTests(TestCase):
    """payment_success / payment_failed must render with every query state."""

    def test_payment_success_no_args(self):
        from clients.views.subscription_views import payment_success
        req = RF.get('/payment/success/')
        resp = payment_success(req)
        self.assertEqual(resp.status_code, 200)

    def test_payment_success_with_args(self):
        from clients.views.subscription_views import payment_success
        req = RF.get('/payment/success/', data={
            'shop': 'foo', 'plan': 'silver', 'period': 'annual',
            'type': 'subscription',
        })
        resp = payment_success(req)
        self.assertEqual(resp.status_code, 200)

    def test_payment_failed_known_reason(self):
        from clients.views.subscription_views import payment_failed
        req = RF.get('/payment/failed/', data={'reason': 'payment_declined'})
        resp = payment_failed(req)
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'payment', resp.content.lower())

    def test_payment_failed_unknown_reason(self):
        from clients.views.subscription_views import payment_failed
        req = RF.get('/payment/failed/', data={'reason': 'spaghetti'})
        resp = payment_failed(req)
        self.assertEqual(resp.status_code, 200)
