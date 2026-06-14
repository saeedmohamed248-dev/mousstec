"""Regression coverage for ``erp_core.ai.credits.consume_tenant_credit``.

The central credit ledger consumes AI-design credits from three sources
in this order:

  1. Plan monthly quota (counted via AILimitTracker rows).
  2. AIBonusGrant (admin-issued bonuses).
  3. TenantDesignTopUp (one-time paid top-ups).

A subtle bug found by inspection (June 2026):

    grant = (
        AIBonusGrant.objects.filter(tenant=tenant, is_active=True)
        .filter(granted_designs__gt=F('consumed_designs'))
        .order_by('granted_at')
        .select_for_update(skip_locked=True)
        .first()
    )
    if grant:
        if not (grant.expires_at and grant.expires_at < now):
            # consume

The QuerySet picks the **oldest** grant with unused designs but does
**not** exclude expired ones. If the tenant's oldest grant is expired
(say: 1 unused design, expired last month) and a brand-new grant has
10 valid designs, the code picks the expired one, finds it expired,
skips the consume, and **drops straight through to the topup path** —
never giving the valid grant a chance. The tenant pays from their
paid top-up instead of using the free admin grant.

``test_expired_grant_does_not_starve_valid_grant`` pins the regression.
Tests are organized so the simpler scenarios run first; the bug case
is last.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.test import TransactionTestCase
from django.utils import timezone

from clients.models import (
    AIBonusGrant,
    AILimitTracker,
    Client,
    Plan,
    TenantDesignTopUp,
    TenantSubscription,
)
from erp_core.ai.credits import (
    consume_tenant_credit,
    get_tenant_balance,
)


def _make_tenant(*, suffix='c') -> Client:
    """Create a public-schema Client and *clear* any auto-provisioned
    AI bonus grants/topups so each test starts with a deterministic
    credit balance.

    The platform-side onboarding signal (``auto_setup_new_tenant``)
    seeds a welcome-bonus grant + a signup-bonus grant the moment the
    Client row lands; without removing them, every balance assertion
    in this file would have to track the auto-grant numbers.
    """
    c = Client(
        schema_name=f'credit_{suffix}',
        name=f'Credit {suffix}',
        owner_name='Owner',
        phone='01000000000',
    )
    c.auto_create_schema = False
    c.save()
    # Wipe any auto-issued credits so the test owns the full balance.
    AIBonusGrant.objects.filter(tenant=c).delete()
    TenantDesignTopUp.objects.filter(tenant=c).delete()
    AILimitTracker.objects.filter(tenant=c).delete()
    return c


def _make_plan_with_quota(*, slug: str, quota: int) -> Plan:
    plan, _ = Plan.objects.update_or_create(
        slug=slug,
        defaults={
            'name': f'Plan {slug}',
            'industry': 'automotive',
            'monthly_price': Decimal('500'),
            'max_branches': 1, 'max_users': 1, 'max_treasuries': 1,
            'monthly_ai_designs_quota': quota,
            'entitlements': {'core_invoicing': {'enabled': True}},
        },
    )
    return plan


class GetTenantBalanceTests(TransactionTestCase):
    """``get_tenant_balance`` reads from three tables and sums.
    Pins the math so a future refactor doesn't drift."""

    def test_returns_zero_for_tenant_without_subscription(self):
        tenant = _make_tenant(suffix='bal_nosub')
        bal = get_tenant_balance(tenant)
        self.assertEqual(bal['plan_quota'], 0)
        self.assertEqual(bal['plan_remaining'], 0)
        self.assertEqual(bal['grants_remaining'], 0)
        self.assertEqual(bal['topups_remaining'], 0)
        self.assertEqual(bal['total'], 0)

    def test_plan_quota_minus_used_this_month(self):
        tenant = _make_tenant(suffix='bal_plan')
        plan = _make_plan_with_quota(slug='bal-plan', quota=50)
        TenantSubscription.objects.create(tenant=tenant, plan=plan)

        # Mark 3 generations as used this month.
        for _ in range(3):
            AILimitTracker.objects.create(
                tenant=tenant, action_type='ai_generation',
            )

        bal = get_tenant_balance(tenant)
        self.assertEqual(bal['plan_quota'], 50)
        self.assertEqual(bal['plan_used_this_month'], 3)
        self.assertEqual(bal['plan_remaining'], 47)

    def test_grants_sum_only_active_and_unexpired(self):
        tenant = _make_tenant(suffix='bal_grant')

        # Active, unexpired, 7 unused.
        AIBonusGrant.objects.create(
            tenant=tenant, granted_designs=10,
            consumed_designs=3, is_active=True,
        )
        # Active but expired — must not count.
        AIBonusGrant.objects.create(
            tenant=tenant, granted_designs=5,
            consumed_designs=0, is_active=True,
            expires_at=timezone.now() - timedelta(days=1),
        )
        # Inactive — must not count.
        AIBonusGrant.objects.create(
            tenant=tenant, granted_designs=20,
            consumed_designs=0, is_active=False,
        )

        bal = get_tenant_balance(tenant)
        self.assertEqual(bal['grants_remaining'], 7)


class ConsumeTenantCreditTests(TransactionTestCase):
    """``consume_tenant_credit`` must follow the documented priority
    order and remain atomic under each path."""

    # ── Plan path ────────────────────────────────────────────────────
    def test_consumes_from_plan_when_quota_available(self):
        tenant = _make_tenant(suffix='cons_plan')
        plan = _make_plan_with_quota(slug='cons-plan', quota=5)
        TenantSubscription.objects.create(tenant=tenant, plan=plan)

        result = consume_tenant_credit(tenant)

        self.assertTrue(result['success'])
        self.assertEqual(result['source'], 'plan')
        self.assertEqual(
            AILimitTracker.objects.filter(tenant=tenant).count(), 1,
        )

    def test_falls_through_when_plan_quota_exhausted(self):
        tenant = _make_tenant(suffix='cons_plan_x')
        plan = _make_plan_with_quota(slug='cons-plan-x', quota=2)
        TenantSubscription.objects.create(tenant=tenant, plan=plan)
        # Burn the plan quota.
        for _ in range(2):
            AILimitTracker.objects.create(
                tenant=tenant, action_type='ai_generation',
            )
        AIBonusGrant.objects.create(
            tenant=tenant, granted_designs=3, is_active=True,
        )

        result = consume_tenant_credit(tenant)

        self.assertTrue(result['success'])
        self.assertEqual(result['source'], 'grant')

    # ── Grant path ───────────────────────────────────────────────────
    def test_grant_consumed_increments_consumed_count(self):
        tenant = _make_tenant(suffix='cons_g')
        grant = AIBonusGrant.objects.create(
            tenant=tenant, granted_designs=4, is_active=True,
        )

        result = consume_tenant_credit(tenant)

        self.assertTrue(result['success'])
        self.assertEqual(result['source'], 'grant')
        grant.refresh_from_db()
        self.assertEqual(grant.consumed_designs, 1)

    def test_oldest_grant_is_drained_first(self):
        """When multiple valid grants exist, the oldest (lowest
        granted_at) should be drained first."""
        tenant = _make_tenant(suffix='cons_g_old')
        old = AIBonusGrant.objects.create(
            tenant=tenant, granted_designs=2,
            consumed_designs=0, is_active=True,
        )
        # Force a created_at via auto_now_add — manually update.
        AIBonusGrant.objects.filter(pk=old.pk).update(
            granted_at=timezone.now() - timedelta(days=7),
        )
        new = AIBonusGrant.objects.create(
            tenant=tenant, granted_designs=5,
            consumed_designs=0, is_active=True,
        )

        consume_tenant_credit(tenant)

        old.refresh_from_db()
        new.refresh_from_db()
        self.assertEqual(old.consumed_designs, 1)
        self.assertEqual(new.consumed_designs, 0)

    # ── Topup path ───────────────────────────────────────────────────
    def test_topup_consumed_after_grants_exhausted(self):
        tenant = _make_tenant(suffix='cons_top')
        # No grants. Just a paid topup.
        TenantDesignTopUp.objects.create(
            tenant=tenant, designs_total=3, designs_used=0,
            price_paid=Decimal('100'), status='paid',
            paid_at=timezone.now(),
        )

        result = consume_tenant_credit(tenant)

        self.assertTrue(result['success'])
        self.assertEqual(result['source'], 'topup')

    # ── No-credit path ───────────────────────────────────────────────
    def test_no_credit_returns_failure_without_creating_tracker(self):
        tenant = _make_tenant(suffix='cons_none')

        result = consume_tenant_credit(tenant)

        self.assertFalse(result['success'])
        self.assertEqual(result['reason'], 'no_credit')
        self.assertEqual(
            AILimitTracker.objects.filter(tenant=tenant).count(), 0,
            'No tracker row should be created on a no-credit failure.',
        )

    # ── BUG REGRESSION: expired grant must not starve valid grants ──
    def test_expired_grant_does_not_starve_valid_grant(self):
        """The bug-fix regression. Setup:

          - Tenant has an OLD AIBonusGrant: 5 granted, 0 consumed,
            expired yesterday.
          - Tenant has a NEW AIBonusGrant: 10 granted, 0 consumed,
            still valid.

        Pre-fix: the QuerySet ordered by granted_at picks the expired
        grant first, the expires_at check skips the consume, and the
        function falls through to the topup path — never trying the
        valid grant. The tenant pays from their topup instead of
        using the free admin grant.

        Post-fix: the QuerySet excludes expired grants, so the valid
        new grant is selected and consumed.
        """
        tenant = _make_tenant(suffix='cons_exp')
        # Old expired grant.
        old = AIBonusGrant.objects.create(
            tenant=tenant, granted_designs=5, consumed_designs=0,
            is_active=True,
            expires_at=timezone.now() - timedelta(days=1),
        )
        AIBonusGrant.objects.filter(pk=old.pk).update(
            granted_at=timezone.now() - timedelta(days=30),
        )
        # New valid grant.
        new = AIBonusGrant.objects.create(
            tenant=tenant, granted_designs=10, consumed_designs=0,
            is_active=True,
        )
        # And a topup as the "wrong answer" fallback.
        TenantDesignTopUp.objects.create(
            tenant=tenant, designs_total=20, designs_used=0,
            price_paid=Decimal('100'), status='paid',
            paid_at=timezone.now(),
        )

        result = consume_tenant_credit(tenant)

        self.assertTrue(result['success'])
        self.assertEqual(
            result['source'], 'grant',
            'Should consume the valid NEW grant, not skip to topup. '
            'The expired old grant must not block the priority order.',
        )
        new.refresh_from_db()
        self.assertEqual(new.consumed_designs, 1)
        old.refresh_from_db()
        self.assertEqual(old.consumed_designs, 0)  # untouched
