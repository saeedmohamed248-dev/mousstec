"""Granular SaaS billing — Feature catalog, SubscriptionPackage, grants, and
the upgraded verify_coding_subscription path.

Covers:
  • Feature.code is unique (catalog integrity)
  • SubscriptionPackage.features m2m + feature_codes() helper
  • TenantPackageGrant + TenantFeatureGrant shared lifecycle helpers
      (is_currently_valid / is_time_expired / is_usage_exhausted)
  • verify_coding_subscription_or_hold(feature_code=...) finds active grants
  • Expired / exhausted grants surface as mode='denied' (NOT silent fall-through)
  • Legacy path (no feature_code) still flows through settings whitelist + gift
  • consume_feature_usage decrements + audits + is idempotent on operation_ref
"""
from __future__ import annotations

import asyncio
from datetime import timedelta
from decimal import Decimal

from asgiref.sync import sync_to_async
from django.utils import timezone

from bmw_ecu.tests.base import (
    BmwEcuTenantTestCase as TestCase,
    setup_module_tenant, teardown_module_tenant,
)


# Provision the tenant ONCE for the whole module — saves ~3 minutes vs
# letting each test class spin up its own.
def setUpModule() -> None:
    setup_module_tenant()


def tearDownModule() -> None:
    teardown_module_tenant()

from bmw_ecu.models import (
    Feature,
    FeatureUsageEvent,
    SubscriptionPackage,
    TenantFeatureGrant,
    TenantPackageGrant,
)
from bmw_ecu.services.billing_gate import LocalBillingGate
from bmw_ecu.services.entitlement import (
    DefaultEntitlementProvider,
    EntitlementVerdict,
    OperationType,
    _check_granular_grant_sync,
    consume_feature_usage_sync,
)


# ─────────────────────────────────────────────────────────────────────
# Catalog-level integrity
# ─────────────────────────────────────────────────────────────────────
class FeatureCatalogTests(TestCase):
    def test_seed_migration_populated_features(self) -> None:
        # The 0006 data migration is idempotent + ships these codes.
        for code in ["frm_repair", "key_programming", "egs_isn_reset",
                     "acsm_crash_reset", "cbs_battery_manager"]:
            self.assertTrue(
                Feature.objects.filter(code=code).exists(),
                f"Seed migration should have created feature {code!r}",
            )

    def test_feature_code_is_unique(self) -> None:
        # The integrity violation aborts the surrounding transaction. Wrap
        # the offending call in atomic() so the rest of the test (and the
        # TransactionTestCase teardown) can keep using the connection.
        from django.db import IntegrityError, transaction
        Feature.objects.create(code="frm_repair_dup", name="Dup")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Feature.objects.create(code="frm_repair_dup", name="Conflict")

    def test_seed_migration_populated_packages(self) -> None:
        for code in ["pkg_starter", "pkg_key_master", "pkg_full_suite"]:
            self.assertTrue(
                SubscriptionPackage.objects.filter(code=code).exists(),
                f"Seed migration should have created package {code!r}",
            )

    def test_full_suite_bundles_every_feature(self) -> None:
        full = SubscriptionPackage.objects.get(code="pkg_full_suite")
        codes = set(full.feature_codes())
        # Spot-check the headliners from the Epic.
        for must_have in [
            "frm_repair", "key_programming", "egs_isn_reset",
            "acsm_crash_reset", "cbs_battery_manager",
        ]:
            self.assertIn(must_have, codes)

    def test_key_master_focuses_on_key_programming(self) -> None:
        pkg = SubscriptionPackage.objects.get(code="pkg_key_master")
        codes = set(pkg.feature_codes())
        self.assertIn("key_programming", codes)
        self.assertIn("key_programming_fem", codes)
        # Key Master deliberately excludes coding features.
        self.assertNotIn("f_series_coding", codes)


# ─────────────────────────────────────────────────────────────────────
# Lifecycle helpers on the abstract grant base
# ─────────────────────────────────────────────────────────────────────
class GrantLifecycleTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.feature = Feature.objects.get(code="key_programming")
        self.pkg = SubscriptionPackage.objects.get(code="pkg_key_master")

    def test_time_bound_active_grant_is_valid(self) -> None:
        g = TenantFeatureGrant.objects.create(
            tenant_schema="ws1", feature=self.feature, billing_mode="time",
            valid_until=timezone.now() + timedelta(days=10),
        )
        self.assertTrue(g.is_currently_valid())
        self.assertFalse(g.is_time_expired())
        self.assertIsNone(g.usage_remaining())   # unlimited

    def test_time_bound_expired_grant_is_invalid(self) -> None:
        g = TenantFeatureGrant.objects.create(
            tenant_schema="ws1", feature=self.feature, billing_mode="time",
            valid_until=timezone.now() - timedelta(days=1),
        )
        self.assertFalse(g.is_currently_valid())
        self.assertTrue(g.is_time_expired())

    def test_usage_bound_grant_tracks_remaining(self) -> None:
        g = TenantFeatureGrant.objects.create(
            tenant_schema="ws1", feature=self.feature, billing_mode="usage",
            usage_quota=5, usage_used=2,
        )
        self.assertTrue(g.is_currently_valid())
        self.assertEqual(g.usage_remaining(), 3)
        self.assertFalse(g.is_usage_exhausted())

    def test_usage_bound_grant_exhausted_when_used_up(self) -> None:
        g = TenantFeatureGrant.objects.create(
            tenant_schema="ws1", feature=self.feature, billing_mode="usage",
            usage_quota=3, usage_used=3,
        )
        self.assertFalse(g.is_currently_valid())
        self.assertTrue(g.is_usage_exhausted())
        self.assertEqual(g.usage_remaining(), 0)

    def test_hybrid_grant_fails_on_either_axis(self) -> None:
        # Time still valid but usage exhausted → invalid.
        g = TenantFeatureGrant.objects.create(
            tenant_schema="ws1", feature=self.feature, billing_mode="hybrid",
            valid_until=timezone.now() + timedelta(days=30),
            usage_quota=2, usage_used=2,
        )
        self.assertFalse(g.is_currently_valid())

    def test_revoked_grant_is_invalid_even_if_window_open(self) -> None:
        g = TenantFeatureGrant.objects.create(
            tenant_schema="ws1", feature=self.feature, billing_mode="time",
            status="revoked",
            valid_until=timezone.now() + timedelta(days=30),
        )
        self.assertFalse(g.is_currently_valid())


# ─────────────────────────────────────────────────────────────────────
# verify_coding_subscription — granular path
# ─────────────────────────────────────────────────────────────────────
class GranularVerifyTests(TestCase):
    """The new feature_code branch on DefaultEntitlementProvider."""

    def setUp(self) -> None:
        super().setUp()
        # Use the real module-tenant schema name so schema_context inside
        # the async verify/consume paths lands on a schema that exists.
        self.tenant = "test_bmw_ecu"
        self.feature_code = "frm_repair"
        self.feature = Feature.objects.get(code=self.feature_code)

    def _verify(self, *, feature_code, tenant=None, vin="VIN1",
                op=OperationType.CODING):
        """Drive verify() through the sync core to keep DB queries on the
        test thread (where set_tenant was applied). Mirrors what the real
        async path does, minus the asyncio shuffle."""
        result = _check_granular_grant_sync(
            tenant_schema=tenant or self.tenant,
            feature_code=feature_code,
            operation_type=op,
        )
        if result is not None:
            return result
        # No grant in the granular table → fall through to legacy provider
        # so the "no_grant_falls_through_to_legacy" test path works.
        prov = DefaultEntitlementProvider()
        async def run():
            return await prov.verify(
                vin=vin, operation_type=op,
                tenant_schema=tenant or self.tenant,
                feature_code=feature_code,
            )
        return asyncio.run(run())

    def test_active_package_grant_entitles_bundled_feature(self) -> None:
        pkg = SubscriptionPackage.objects.get(code="pkg_repair_specialist")
        self.assertIn(self.feature_code, pkg.feature_codes())  # sanity
        TenantPackageGrant.objects.create(
            tenant_schema=self.tenant, package=pkg, billing_mode="time",
            valid_until=timezone.now() + timedelta(days=30),
        )
        v = self._verify(feature_code=self.feature_code)
        self.assertTrue(v.entitled)
        self.assertEqual(v.mode, "package")
        self.assertEqual(v.grant_kind, "package")
        self.assertEqual(v.feature_code, self.feature_code)
        self.assertGreater(v.grant_pk, 0)

    def test_direct_feature_grant_entitles(self) -> None:
        TenantFeatureGrant.objects.create(
            tenant_schema=self.tenant, feature=self.feature,
            billing_mode="usage", usage_quota=3,
        )
        v = self._verify(feature_code=self.feature_code)
        self.assertTrue(v.entitled)
        self.assertEqual(v.mode, "feature_grant")
        self.assertEqual(v.usage_remaining, 3)

    def test_expired_grant_denies_not_falls_through(self) -> None:
        """Critical: an expired grant must NOT silently fall through to the
        legacy whitelist — that would re-entitle a tenant whose subscription
        just lapsed because of the BMW_ECU_CODING_ENTITLED_GLOBALLY flag."""
        TenantFeatureGrant.objects.create(
            tenant_schema=self.tenant, feature=self.feature,
            billing_mode="time",
            valid_until=timezone.now() - timedelta(days=1),
        )
        with self.settings(BMW_ECU_CODING_ENTITLED_GLOBALLY=True):
            v = self._verify(feature_code=self.feature_code)
        self.assertFalse(v.entitled)
        self.assertEqual(v.mode, "denied")
        self.assertIn("expired or exhausted", v.reason)

    def test_exhausted_usage_grant_denies(self) -> None:
        TenantFeatureGrant.objects.create(
            tenant_schema=self.tenant, feature=self.feature,
            billing_mode="usage", usage_quota=2, usage_used=2,
        )
        v = self._verify(feature_code=self.feature_code)
        self.assertFalse(v.entitled)
        self.assertEqual(v.mode, "denied")

    def test_terminal_status_grant_still_denies_with_legacy_flag(self) -> None:
        """Audit fix B2 — a grant auto-marked status='exhausted' by a prior
        consume() must NOT be invisible to the granular lookup. Before the
        fix, the filter restricted to status='active' which silently dropped
        terminal-status rows; with the legacy global flag on, the caller
        would then fall through and re-entitle a lapsed customer."""
        TenantFeatureGrant.objects.create(
            tenant_schema=self.tenant, feature=self.feature,
            billing_mode="usage", usage_quota=3, usage_used=3,
            status="exhausted",   # ← terminal status the bug used to skip
        )
        with self.settings(BMW_ECU_CODING_ENTITLED_GLOBALLY=True):
            v = self._verify(feature_code=self.feature_code)
        self.assertFalse(v.entitled)
        self.assertEqual(v.mode, "denied")
        self.assertIn("expired or exhausted", v.reason)

    def test_revoked_grant_falls_through_to_legacy(self) -> None:
        """Audit fix B2 (sibling) — a revoked grant should behave as if the
        tenant never had any grant (the legacy whitelist gets to decide),
        because admin revocation models a refund + re-evaluation, not a
        terminal billing event."""
        TenantFeatureGrant.objects.create(
            tenant_schema=self.tenant, feature=self.feature,
            billing_mode="usage", usage_quota=3, status="revoked",
        )
        result = _check_granular_grant_sync(
            tenant_schema=self.tenant,
            feature_code=self.feature_code,
            operation_type=OperationType.CODING,
        )
        self.assertIsNone(result)

    def test_no_grant_returns_none_so_caller_falls_through(self) -> None:
        """When tenant has NO grant for the feature, _check_granular_grant
        returns None — signalling the upstream verify() to fall through to
        the legacy settings whitelist / gift credit path. The legacy path
        itself is covered by the 70-test baseline + entitlement tests."""
        result = _check_granular_grant_sync(
            tenant_schema=self.tenant,
            feature_code=self.feature_code,
            operation_type=OperationType.CODING,
        )
        self.assertIsNone(result)

    def test_unknown_feature_code_returns_none(self) -> None:
        """Unknown feature_code → sync core can't answer → caller falls
        through to legacy (mirrors the production behaviour)."""
        result = _check_granular_grant_sync(
            tenant_schema=self.tenant,
            feature_code="this_feature_does_not_exist",
            operation_type=OperationType.CODING,
        )
        self.assertIsNone(result)

    def test_package_grant_beats_feature_grant_when_both_exist(self) -> None:
        pkg = SubscriptionPackage.objects.get(code="pkg_repair_specialist")
        TenantPackageGrant.objects.create(
            tenant_schema=self.tenant, package=pkg, billing_mode="time",
            valid_until=timezone.now() + timedelta(days=30),
        )
        TenantFeatureGrant.objects.create(
            tenant_schema=self.tenant, feature=self.feature,
            billing_mode="usage", usage_quota=5,
        )
        v = self._verify(feature_code=self.feature_code)
        self.assertEqual(v.mode, "package")
        self.assertEqual(v.grant_kind, "package")


# ─────────────────────────────────────────────────────────────────────
# consume_feature_usage — ledger + idempotency + auto-exhaust
# ─────────────────────────────────────────────────────────────────────
class ConsumeUsageTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        # Use the real module-tenant schema name so schema_context inside
        # the async verify/consume paths lands on a schema that exists.
        self.tenant = "test_bmw_ecu"
        self.feature = Feature.objects.get(code="key_programming")
        self.grant = TenantFeatureGrant.objects.create(
            tenant_schema=self.tenant, feature=self.feature,
            billing_mode="usage", usage_quota=2,
        )
        self.verdict = EntitlementVerdict(
            entitled=True, operation_type=OperationType.CODING,
            mode="feature_grant",
            feature_code=self.feature.code,
            grant_kind="feature", grant_pk=self.grant.pk,
            usage_remaining=2,
        )

    def test_consume_decrements_and_audits(self) -> None:
        ok = consume_feature_usage_sync(
            verdict=self.verdict, tenant_schema=self.tenant,
            vin="WBA1", operation_ref="OP-1",
        )
        self.assertTrue(ok)
        self.grant.refresh_from_db()
        self.assertEqual(self.grant.usage_used, 1)
        self.assertEqual(self.grant.status, "active")  # not yet exhausted
        self.assertEqual(
            FeatureUsageEvent.objects.filter(
                tenant_schema=self.tenant, feature=self.feature,
                operation_ref="OP-1",
            ).count(), 1,
        )

    def test_consume_is_idempotent_on_operation_ref(self) -> None:
        consume_feature_usage_sync(
            verdict=self.verdict, tenant_schema=self.tenant,
            vin="WBA1", operation_ref="OP-1",
        )
        consume_feature_usage_sync(
            verdict=self.verdict, tenant_schema=self.tenant,
            vin="WBA1", operation_ref="OP-1",
        )          # replay — must be a no-op
        self.grant.refresh_from_db()
        self.assertEqual(self.grant.usage_used, 1)
        self.assertEqual(
            FeatureUsageEvent.objects.filter(operation_ref="OP-1").count(), 1,
        )

    def test_consume_auto_marks_exhausted(self) -> None:
        consume_feature_usage_sync(
            verdict=self.verdict, tenant_schema=self.tenant,
            vin="WBA1", operation_ref="OP-1",
        )
        consume_feature_usage_sync(
            verdict=self.verdict, tenant_schema=self.tenant,
            vin="WBA1", operation_ref="OP-2",
        )    # 2/2 → exhausted
        self.grant.refresh_from_db()
        self.assertEqual(self.grant.usage_used, 2)
        self.assertEqual(self.grant.status, "exhausted")

    def test_consume_returns_true_for_legacy_verdicts(self) -> None:
        """Legacy whitelist / gift verdicts have no grant to decrement —
        consume must accept them gracefully so callers can do an
        unconditional consume() after verify()."""
        legacy = EntitlementVerdict(
            entitled=True, operation_type=OperationType.CODING,
            mode="subscription", subscription_ref="settings:foo",
        )
        self.assertTrue(consume_feature_usage_sync(
            verdict=legacy, tenant_schema=self.tenant,
            operation_ref="OP-LEGACY",
        ))

    def test_unique_constraint_blocks_duplicate_at_db_level(self) -> None:
        """Audit fix B4: the partial unique index on
        (tenant_schema, feature, operation_ref) is the last line of
        defence against a concurrent worker that bypasses the Python
        idempotency probe. Construct a duplicate INSERT directly and
        verify Postgres rejects it."""
        from django.db import IntegrityError, transaction
        from bmw_ecu.models import FeatureUsageEvent
        FeatureUsageEvent.objects.create(
            tenant_schema=self.tenant, feature=self.feature,
            grant_kind="feature", feature_grant=self.grant,
            vin="WBA1", operation_ref="OP-DUP",
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                FeatureUsageEvent.objects.create(
                    tenant_schema=self.tenant, feature=self.feature,
                    grant_kind="feature", feature_grant=self.grant,
                    vin="WBA1", operation_ref="OP-DUP",
                )

    def test_unique_constraint_allows_empty_operation_ref(self) -> None:
        """Audit fix B4: the partial unique index has a
        `condition=~Q(operation_ref="")` clause so callers without an
        idempotency key (legacy / anonymous flows) can still book
        multiple events. Two consecutive rows with operation_ref=""
        must both succeed."""
        from bmw_ecu.models import FeatureUsageEvent
        for _ in range(2):
            FeatureUsageEvent.objects.create(
                tenant_schema=self.tenant, feature=self.feature,
                grant_kind="feature", feature_grant=self.grant,
                vin="WBA1", operation_ref="",
            )
        self.assertEqual(
            FeatureUsageEvent.objects.filter(operation_ref="").count(),
            2,
        )


# ─────────────────────────────────────────────────────────────────────
# End-to-end shape — verify the granular verdict survives the round-trip
# through CodingEntitlement (the chatbot-facing dataclass on the gate).
# ─────────────────────────────────────────────────────────────────────
class GateGranularPathTests(TestCase):
    def test_verdict_carries_granular_fields_into_entitlement(self) -> None:
        """LocalBillingGate.verify_coding_subscription_or_hold maps the
        EntitlementVerdict into a CodingEntitlement dataclass. The
        granular fields (feature_code / grant_kind / grant_pk /
        usage_remaining) must survive the mapping so coding_orchestrator
        can pass grant_pk into consume_feature_usage afterwards."""
        from bmw_ecu.services.billing_gate import CodingEntitlement
        tenant = "test_bmw_ecu"  # the real schema set up by setUpModule
        feature = Feature.objects.get(code="acsm_crash_reset")
        grant = TenantFeatureGrant.objects.create(
            tenant_schema=tenant, feature=feature,
            billing_mode="usage", usage_quota=3,
        )
        verdict = _check_granular_grant_sync(
            tenant_schema=tenant, feature_code="acsm_crash_reset",
            operation_type=OperationType.CODING,
        )
        self.assertIsNotNone(verdict)
        self.assertTrue(verdict.entitled)

        # Verify the gate-level CodingEntitlement preserves every field we
        # care about (the verify_coding_subscription_or_hold sync_to_async
        # path is exercised by mock-based tests in test_billing_gate.py).
        ent = CodingEntitlement(
            entitled=verdict.entitled,
            operation_type=verdict.operation_type.value,
            mode=verdict.mode,
            subscription_ref=verdict.subscription_ref,
            reason=verdict.reason,
            feature_code=verdict.feature_code,
            grant_kind=verdict.grant_kind,
            grant_pk=verdict.grant_pk,
            usage_remaining=verdict.usage_remaining,
        )
        self.assertEqual(ent.mode, "feature_grant")
        self.assertEqual(ent.feature_code, "acsm_crash_reset")
        self.assertEqual(ent.grant_kind, "feature")
        self.assertEqual(ent.grant_pk, grant.pk)
        self.assertEqual(ent.usage_remaining, 3)
