"""Regression coverage for Plan revision + TenantSubscription snapshot.

These two signals (``clients/signals.py``) are the load-bearing piece
of the grandfathering story:

* ``auto_create_plan_revision`` — every time a Plan's price or
  entitlements change, a PlanRevision row is appended so historians
  (and SuperAdmin force-apply actions) can see exactly what each
  tenant was promised when they signed up.
* ``auto_snapshot_new_subscription`` — every freshly-created
  TenantSubscription copies the current Plan price + entitlements
  into ``locked_*`` fields so a later Plan price hike (or
  entitlement removal) doesn't retroactively change what the
  tenant is paying for.

A silent regression here = revenue leak (tenants billed at the new
price they never accepted) or revenue loss (snapshots not made,
so a downgraded plan retroactively shrinks paid features). Both
are months-to-detect failures because no exception fires — the
ledger just drifts.

Coverage:

* PlanRevision is created exactly once per actual price/entitlement
  change. No-op saves don't append.
* TenantSubscription gets a snapshot on create, including locked
  price and a frozen copy of plan.entitlements.
* Updating an existing subscription does NOT re-snapshot — locked_*
  must stay at the create-time values.
* ``effective_entitlements`` prefers the snapshot once locked_at is
  set, so a Plan-side change after the snapshot is invisible to
  the tenant until SuperAdmin re-snapshots.
"""
from __future__ import annotations

from decimal import Decimal

from django.test import TransactionTestCase

from clients.models import (
    Client,
    Plan,
    PlanRevision,
    TenantSubscription,
)


def _make_tenant(suffix: str) -> Client:
    """Create a public Client without provisioning a tenant schema —
    keeps these tests fast. Only the public-schema tables matter
    for revision/snapshot logic.
    """
    c = Client(
        schema_name=f'rev_{suffix}',
        name=f'Tenant {suffix}',
        owner_name='Owner',
        phone='01000000000',
    )
    c.auto_create_schema = False
    c.save()
    return c


def _make_plan(slug: str, *, price=Decimal('500'), entitlements=None) -> Plan:
    plan, _ = Plan.objects.update_or_create(
        slug=slug,
        defaults={
            'name': f'Plan {slug}',
            'industry': 'automotive',
            'monthly_price': price,
            'max_branches': 1, 'max_users': 1, 'max_treasuries': 1,
            'entitlements': entitlements or {'core_invoicing': {'enabled': True}},
        },
    )
    return plan


class PlanRevisionSignalTests(TransactionTestCase):
    """``auto_create_plan_revision`` must log exactly one revision per
    real change — and stay quiet on no-op saves."""

    def test_initial_revision_created_on_first_save(self):
        plan = _make_plan('rev-initial', price=Decimal('500'))
        revs = PlanRevision.objects.filter(plan=plan)
        self.assertEqual(revs.count(), 1)
        self.assertEqual(revs.first().monthly_price, Decimal('500.00'))

    def test_price_change_appends_revision(self):
        plan = _make_plan('rev-price', price=Decimal('500'))
        plan.monthly_price = Decimal('600')
        plan.save()
        self.assertEqual(PlanRevision.objects.filter(plan=plan).count(), 2)
        latest = PlanRevision.latest_for(plan)
        self.assertEqual(latest.monthly_price, Decimal('600.00'))
        self.assertIn('500', latest.change_reason)
        self.assertIn('600', latest.change_reason)

    def test_entitlement_change_appends_revision(self):
        plan = _make_plan('rev-ent', entitlements={
            'core_invoicing': {'enabled': True},
        })
        plan.entitlements = {
            'core_invoicing':   {'enabled': True},
            'b2b_marketplace':  {'enabled': True},
        }
        plan.save()
        self.assertEqual(PlanRevision.objects.filter(plan=plan).count(), 2)
        latest = PlanRevision.latest_for(plan)
        self.assertIn('entitlements', latest.change_reason)
        self.assertIn('b2b_marketplace', latest.entitlements)

    def test_no_op_save_does_not_append_revision(self):
        """Save() with no actual change — name edit, unrelated field —
        must NOT create a revision. Otherwise the history is noise
        and force-apply UIs become useless."""
        plan = _make_plan('rev-noop', price=Decimal('500'))
        plan.name = 'Renamed for marketing'  # not a versioned field
        plan.save()
        self.assertEqual(PlanRevision.objects.filter(plan=plan).count(), 1)

    def test_change_reason_includes_both_when_both_change(self):
        plan = _make_plan('rev-both', price=Decimal('500'))
        plan.monthly_price = Decimal('700')
        plan.entitlements = {'core_invoicing': {'enabled': False}}
        plan.save()
        latest = PlanRevision.latest_for(plan)
        self.assertIn('price', latest.change_reason)
        self.assertIn('entitlements', latest.change_reason)


class SubscriptionSnapshotSignalTests(TransactionTestCase):
    """``auto_snapshot_new_subscription`` is the grandfathering hook —
    a tenant signed at price X with entitlements E must stay at
    (X, E) until SuperAdmin explicitly re-snapshots, regardless of
    later Plan edits."""

    def test_snapshot_taken_on_create(self):
        plan = _make_plan('snap-1', price=Decimal('555'), entitlements={
            'core_invoicing': {'enabled': True},
            'reports_basic':  {'enabled': True},
        })
        tenant = _make_tenant('snap1')

        sub = TenantSubscription.objects.create(tenant=tenant, plan=plan)
        sub.refresh_from_db()

        self.assertIsNotNone(sub.locked_at)
        self.assertEqual(sub.locked_monthly_price, Decimal('555.00'))
        self.assertEqual(sub.locked_entitlements, {
            'core_invoicing': {'enabled': True},
            'reports_basic':  {'enabled': True},
        })

    def test_snapshot_not_taken_when_no_plan(self):
        tenant = _make_tenant('snap2')
        sub = TenantSubscription.objects.create(tenant=tenant, plan=None)
        sub.refresh_from_db()
        self.assertIsNone(sub.locked_at)
        self.assertIsNone(sub.locked_monthly_price)

    def test_update_does_not_re_snapshot(self):
        """Editing an existing subscription must leave locked_* alone.
        The only ways to re-snapshot are SuperAdmin's force-apply or
        the renewal webhook (Phase 4) — both call snapshot_from_plan
        explicitly. Otherwise a routine admin edit (e.g. extending
        billing_cycle_months) would silently re-price the tenant."""
        plan = _make_plan('snap-update', price=Decimal('400'))
        tenant = _make_tenant('snap3')
        sub = TenantSubscription.objects.create(tenant=tenant, plan=plan)
        original_locked_at = sub.locked_at
        original_price = sub.locked_monthly_price

        # Mutate the live Plan after the snapshot.
        plan.monthly_price = Decimal('900')
        plan.save()

        # Edit a non-pricing field on the subscription. Snapshot must
        # NOT refresh from the new plan price.
        sub.billing_cycle_months = 3
        sub.save()
        sub.refresh_from_db()

        self.assertEqual(sub.locked_at, original_locked_at)
        self.assertEqual(sub.locked_monthly_price, original_price)
        self.assertEqual(sub.effective_monthly_price, Decimal('400.00'))

    def test_effective_entitlements_uses_snapshot_after_plan_change(self):
        """The whole point of the snapshot: changing plan.entitlements
        after a sub is locked must NOT change effective_entitlements
        for that subscription."""
        plan = _make_plan('snap-ent', entitlements={
            'core_invoicing': {'enabled': True},
        })
        tenant = _make_tenant('snap4')
        sub = TenantSubscription.objects.create(tenant=tenant, plan=plan)

        # Grant a new feature on the Plan — current customers
        # shouldn't get it for free.
        plan.entitlements = {
            'core_invoicing':  {'enabled': True},
            'b2b_marketplace': {'enabled': True},
        }
        plan.save()

        sub.refresh_from_db()
        eff = sub.effective_entitlements
        self.assertIn('core_invoicing', eff)
        self.assertNotIn(
            'b2b_marketplace', eff,
            'Snapshot must shield existing subscriptions from a '
            'post-signup entitlement grant on the Plan.',
        )
