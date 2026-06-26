"""Entitlement check — separates "Coding" subscription from "ISN" pay-per-use.

The ISN flow is pay-per-success (450 EGP captured per VIN, billed via
DiagnosticFeeCharge). The Coding flow is subscription-gated: the
workshop either has the Coding add-on active for this tenant (then
proceed for free at the per-action level) OR a hold is placed for a
future per-action capture once pricing is finalised.

No prices are hardcoded here — the verdict shape supports both:

    mode="subscription"   → entitled via active subscription, no fee.
    mode="hold"           → not subscribed; a hold was placed for later
                            capture once the operation succeeds.
    mode="denied"         → no entitlement, no hold could be placed.

Production provider hits TenantSubscription (or a feature-flag table);
the default provider in this module is environment-driven so the API
keeps working in tests + staging without a subscription model coupling.
"""
from __future__ import annotations

import abc
import enum
import secrets
from dataclasses import dataclass
from typing import Optional

from asgiref.sync import sync_to_async

from ..logging_setup import get_logger

log = get_logger(__name__)


class OperationType(str, enum.Enum):
    """High-level kind of work — gates the billing path."""
    ISN = "isn"             # pay-per-success 450 EGP
    CODING = "coding"       # subscription / hold (no fixed price yet)


@dataclass(frozen=True)
class EntitlementVerdict:
    entitled: bool
    operation_type: OperationType
    mode: str                    # "subscription" | "hold" | "denied" | "package" | "feature_grant" | "gift"
    subscription_ref: str = ""   # provider-specific id (TenantSubscription pk, etc.)
    hold_ref: str = ""           # idempotency key when mode="hold"
    reason: str = ""
    feature_code: str = ""       # set when verify() was called with a granular feature_code
    grant_kind: str = ""         # "package" | "feature" — which grant table answered, if any
    grant_pk: int = 0            # row id of the matching TenantPackageGrant / TenantFeatureGrant
    usage_remaining: int | None = None  # None = unlimited, else int >= 0


class AbstractEntitlementProvider(abc.ABC):
    """One provider per environment. All implementations MUST be idempotent
    against (vin, operation_type, feature_code) — chatbot retries are safe.

    `feature_code` is the granular axis introduced by the SubscriptionPackage
    epic. When None, the provider runs the legacy ISN-vs-Coding logic for
    backward compatibility with every existing call site.
    """

    @abc.abstractmethod
    async def verify(self, *, vin: str,
                     operation_type: OperationType,
                     tenant_schema: Optional[str] = None,
                     feature_code: Optional[str] = None,
                     ) -> EntitlementVerdict: ...


# ---------------------------------------------------------------------------
# Default — env-driven, no subscription-model coupling.
# Production swaps in a TenantSubscriptionEntitlementProvider that reads
# the workshop's active add-ons. Wire via BMW_ECU_ENTITLEMENT_PROVIDER.
# ---------------------------------------------------------------------------
class DefaultEntitlementProvider(AbstractEntitlementProvider):
    """Reads from a Django settings whitelist for the Coding add-on.

    Settings keys (any one enables Coding for the active tenant):
        BMW_ECU_CODING_ENTITLED_TENANTS = {'workshop_a', 'workshop_b'}
        BMW_ECU_CODING_ENTITLED_GLOBALLY = True
    """

    async def verify(self, *, vin: str,
                     operation_type: OperationType,
                     tenant_schema: Optional[str] = None,
                     feature_code: Optional[str] = None,
                     ) -> EntitlementVerdict:
        if operation_type == OperationType.ISN and not feature_code:
            # ISN is always entitled at this layer — the fee gate handles it.
            # (When the caller supplies a feature_code we still want the
            # granular grant lookup to run — some ISN-family features like
            # egs_isn_reset ARE gated by a package.)
            return EntitlementVerdict(
                entitled=True, operation_type=operation_type,
                mode="subscription", reason="ISN flow uses pay-per-success",
            )

        # Coding path.
        from django.conf import settings
        from .gift_credits import GIFT_TYPES_FOR_CODING, has_active_gift

        global_on = bool(getattr(settings, "BMW_ECU_CODING_ENTITLED_GLOBALLY",
                                 False))
        whitelist = set(getattr(settings, "BMW_ECU_CODING_ENTITLED_TENANTS",
                                set()))
        schema = tenant_schema or await _current_schema()

        # ── NEW: granular feature-level grant lookup ───────────────────
        # When the caller asks for a specific feature_code, package and
        # feature grants take precedence over the coarse-grained settings
        # whitelist. Returning early with a structured verdict means audit
        # rows + usage tracking carry the right grant_pk for the consumer
        # to decrement after a successful op.
        if feature_code and schema:
            granular = await _check_granular_grant(
                tenant_schema=schema, feature_code=feature_code,
                operation_type=operation_type,
            )
            if granular is not None:
                return granular

        # Gift entitlement check — promotional grants beat the whitelist.
        if schema:
            gift_pk = await has_active_gift(
                tenant_schema=schema, grant_types=GIFT_TYPES_FOR_CODING,
            )
            if gift_pk is not None:
                return EntitlementVerdict(
                    entitled=True, operation_type=operation_type,
                    mode="gift",
                    subscription_ref=f"gift:{gift_pk}",
                    reason="active Mousstec promotional gift",
                )

        if global_on or (schema and schema in whitelist):
            return EntitlementVerdict(
                entitled=True, operation_type=operation_type,
                mode="subscription",
                subscription_ref=f"settings:{schema or 'global'}",
                reason="active Coding add-on",
            )

        # Not subscribed — place a soft hold so the audit trail exists when
        # we finalise pricing. Hold ref is idempotent per (vin,op).
        hold_ref = await self._issue_hold(vin=vin,
                                          operation_type=operation_type,
                                          schema=schema)
        return EntitlementVerdict(
            entitled=False, operation_type=operation_type,
            mode="hold", hold_ref=hold_ref,
            reason="Coding add-on not active for tenant — hold issued",
        )

    @staticmethod
    @sync_to_async
    def _issue_hold(*, vin: str, operation_type: OperationType,
                    schema: Optional[str]) -> str:
        from django.db import transaction
        from ..models import CodingEntitlementHold

        with transaction.atomic():
            existing = (CodingEntitlementHold.objects
                        .select_for_update()
                        .filter(vin=vin,
                                operation_type=operation_type.value,
                                status="open")
                        .first())
            if existing is not None:
                return existing.hold_ref
            ref = f"HOLD-{secrets.token_urlsafe(10)}"
            CodingEntitlementHold.objects.create(
                vin=vin, operation_type=operation_type.value,
                tenant_schema=schema or "", hold_ref=ref, status="open",
            )
            return ref


@sync_to_async
def _current_schema() -> Optional[str]:
    from django.db import connection
    tenant = getattr(connection, "tenant", None)
    return getattr(tenant, "schema_name", None) if tenant else None


# ---------------------------------------------------------------------------
# Granular grant lookup — runs ONLY when verify() was called with feature_code.
# Returns a fully-formed EntitlementVerdict, or None to fall through to the
# legacy (settings whitelist / gift credit) check.
#
# Split into _sync core + sync_to_async wrapper so tests can exercise the
# DB logic without paying for an event loop, and so production code paths
# that ARE already sync (Django REST views) can call the underlying
# function directly.
# ---------------------------------------------------------------------------
def _check_granular_grant_sync(*, tenant_schema: str, feature_code: str,
                               operation_type: OperationType,
                               ) -> Optional[EntitlementVerdict]:
    """Look for an active TenantPackageGrant containing the requested feature,
    then for a direct TenantFeatureGrant. First valid match wins.

    Returns:
      - EntitlementVerdict(entitled=True, ...) when a grant is found AND
        currently valid (time + usage check).
      - EntitlementVerdict(entitled=False, mode='denied', ...) when a grant
        exists for the tenant + feature but is expired/exhausted/revoked.
      - None when the tenant has no grant for this feature at all → caller
        falls through to legacy settings whitelist / gift credit logic.
    """
    from ..models import (
        Feature, TenantFeatureGrant, TenantPackageGrant,
    )

    # Resolve the feature once. Unknown code → cannot answer at this layer.
    try:
        feature = Feature.objects.get(code=feature_code, is_active=True)
    except Feature.DoesNotExist:
        return None

    # 1️⃣ Package grants take precedence (they're the "bundle" purchase).
    # ⚠️ We INCLUDE non-revoked terminal statuses (expired / exhausted) in
    # the filter — otherwise a grant auto-marked 'exhausted' by a previous
    # consume() would disappear from the result set, `saw_expired_or_exhausted`
    # would stay False, and the caller would fall through to the legacy
    # global-whitelist path — silently RE-ENTITLING a tenant whose
    # subscription just ran out. Revoked grants are excluded so an admin
    # revocation has the same effect as "never bought it" (legacy decides).
    NON_REVOKED = ("active", "expired", "exhausted")
    pkg_grants = (TenantPackageGrant.objects
                  .filter(tenant_schema=tenant_schema,
                          status__in=NON_REVOKED,
                          package__features=feature,
                          package__is_active=True)
                  .select_related("package")
                  .order_by("-granted_at"))

    saw_expired_or_exhausted = False
    for g in pkg_grants:
        if g.is_currently_valid():
            return EntitlementVerdict(
                entitled=True, operation_type=operation_type,
                mode="package",
                subscription_ref=f"pkg:{g.package.code}#{g.pk}",
                reason=f"active package {g.package.code}",
                feature_code=feature_code,
                grant_kind="package", grant_pk=g.pk,
                usage_remaining=g.usage_remaining(),
            )
        saw_expired_or_exhausted = True

    # 2️⃣ Direct single-feature grants — same non-revoked filter rationale.
    feat_grants = (TenantFeatureGrant.objects
                   .filter(tenant_schema=tenant_schema,
                           status__in=NON_REVOKED,
                           feature=feature)
                   .order_by("-granted_at"))
    for g in feat_grants:
        if g.is_currently_valid():
            return EntitlementVerdict(
                entitled=True, operation_type=operation_type,
                mode="feature_grant",
                subscription_ref=f"feat:{feature.code}#{g.pk}",
                reason=f"active feature grant {feature.code}",
                feature_code=feature_code,
                grant_kind="feature", grant_pk=g.pk,
                usage_remaining=g.usage_remaining(),
            )
        saw_expired_or_exhausted = True

    if saw_expired_or_exhausted:
        # Tenant had a grant once — be explicit about WHY it's denied
        # instead of falling through to the legacy whitelist (which would
        # accidentally re-entitle them if they're on the global flag).
        return EntitlementVerdict(
            entitled=False, operation_type=operation_type,
            mode="denied",
            reason=f"grant for {feature_code} is expired or exhausted",
            feature_code=feature_code,
        )

    # No grant of any kind for this tenant + feature → let legacy decide.
    return None


@sync_to_async
def _check_granular_grant(*, tenant_schema: str, feature_code: str,
                          operation_type: OperationType,
                          ) -> Optional[EntitlementVerdict]:
    """Async wrapper. sync_to_async runs the body on a dedicated worker
    thread whose `connection` object is initialised on the *public*
    schema, not the tenant schema the asyncio caller set. Use
    django_tenants.schema_context to bracket the DB work so the worker's
    connection is switched to the tenant schema for the duration of the
    query and restored afterwards."""
    if not tenant_schema:
        return _check_granular_grant_sync(
            tenant_schema=tenant_schema,
            feature_code=feature_code,
            operation_type=operation_type,
        )
    try:
        from django_tenants.utils import schema_context
    except ImportError:
        # Non-tenant install (very unusual at runtime) — fall through.
        return _check_granular_grant_sync(
            tenant_schema=tenant_schema,
            feature_code=feature_code,
            operation_type=operation_type,
        )
    with schema_context(tenant_schema):
        return _check_granular_grant_sync(
            tenant_schema=tenant_schema,
            feature_code=feature_code,
            operation_type=operation_type,
        )


def consume_feature_usage_sync(*, verdict: EntitlementVerdict,
                               tenant_schema: str,
                               vin: str = "",
                               operation_ref: str = "",
                               ) -> bool:
    """Atomically decrement the matching grant's usage counter + write an
    audit row to FeatureUsageEvent.

    Idempotency: caller must pass a stable `operation_ref` (e.g. the
    DiagnosticFeeCharge.authorization_ref). A second call with the same
    (tenant, feature, operation_ref) is a no-op and returns True.

    Returns False only when the grant disappeared between verify() and
    consume() — caller should re-verify and decide whether to roll back.
    """
    from django.db import transaction
    from django.utils import timezone

    from ..models import (
        Feature, FeatureUsageEvent,
        TenantFeatureGrant, TenantPackageGrant,
    )

    if not verdict.entitled or verdict.grant_kind not in ("package", "feature"):
        # Nothing to consume — legacy paths (settings whitelist / gift)
        # are tracked through their own ledgers (GiftCredit / settings).
        return True

    feature = Feature.objects.filter(code=verdict.feature_code).first()
    if feature is None:
        return False

    Model = (TenantPackageGrant if verdict.grant_kind == "package"
             else TenantFeatureGrant)
    from django.db import IntegrityError
    try:
        with transaction.atomic():
            grant = (Model.objects
                     .select_for_update()
                     .filter(pk=verdict.grant_pk).first())
            if grant is None:
                return False

            # ── Idempotency probe — INSIDE the transaction so a concurrent
            # consume() with the same operation_ref racing through select_for_update
            # cannot double-book. The partial unique index defined on
            # FeatureUsageEvent (tenant_schema, feature, operation_ref) is the
            # ultimate safety net — even if a worker bypasses this probe, the
            # INSERT will fail with IntegrityError below.
            if operation_ref and FeatureUsageEvent.objects.filter(
                tenant_schema=tenant_schema, feature=feature,
                operation_ref=operation_ref,
            ).exists():
                return True

            # Re-check validity under lock — race with another consumer.
            if not grant.is_currently_valid():
                return False

            grant.usage_used = (grant.usage_used or 0) + 1
            # Auto-transition the grant into exhausted/expired so subsequent
            # verifies short-circuit instead of touching the row again.
            if grant.is_usage_exhausted():
                grant.status = "exhausted"
            elif grant.is_time_expired():
                grant.status = "expired"
            grant.save(update_fields=["usage_used", "status", "updated_at"])

            FeatureUsageEvent.objects.create(
                tenant_schema=tenant_schema, feature=feature,
                grant_kind=verdict.grant_kind,
                package_grant=grant if verdict.grant_kind == "package" else None,
                feature_grant=grant if verdict.grant_kind == "feature" else None,
                vin=vin, operation_ref=operation_ref,
            )
        return True
    except IntegrityError:
        # The partial unique index on (tenant_schema, feature, operation_ref)
        # rejected the INSERT — another worker already recorded this exact
        # operation_ref. That's the idempotency contract honouring itself.
        return True


@sync_to_async
def consume_feature_usage(*, verdict: EntitlementVerdict,
                          tenant_schema: str,
                          vin: str = "",
                          operation_ref: str = "",
                          ) -> bool:
    """Async wrapper around consume_feature_usage_sync. Uses
    django_tenants.schema_context so the worker thread's connection lands
    on the tenant schema for the duration of the consume."""
    if not tenant_schema:
        return consume_feature_usage_sync(
            verdict=verdict, tenant_schema=tenant_schema,
            vin=vin, operation_ref=operation_ref,
        )
    try:
        from django_tenants.utils import schema_context
    except ImportError:
        return consume_feature_usage_sync(
            verdict=verdict, tenant_schema=tenant_schema,
            vin=vin, operation_ref=operation_ref,
        )
    with schema_context(tenant_schema):
        return consume_feature_usage_sync(
            verdict=verdict, tenant_schema=tenant_schema,
            vin=vin, operation_ref=operation_ref,
        )


# ---------------------------------------------------------------------------
# Test double
# ---------------------------------------------------------------------------
class MockEntitlementProvider(AbstractEntitlementProvider):
    """In-memory test double. Configure which VIN+op combinations are
    entitled, denied, or held."""

    def __init__(self, *,
                 entitled: Optional[set[tuple[str, OperationType]]] = None,
                 hold_for: Optional[set[tuple[str, OperationType]]] = None,
                 denied: Optional[set[tuple[str, OperationType]]] = None) -> None:
        self._entitled = entitled or set()
        self._hold = hold_for or set()
        self._denied = denied or set()
        self._holds_issued: dict[tuple[str, OperationType], str] = {}

    async def verify(self, *, vin: str,
                     operation_type: OperationType,
                     tenant_schema: Optional[str] = None,
                     feature_code: Optional[str] = None,
                     ) -> EntitlementVerdict:
        key = (vin, operation_type)
        if key in self._entitled:
            return EntitlementVerdict(
                entitled=True, operation_type=operation_type,
                mode="subscription", subscription_ref="MOCK-SUB",
            )
        if key in self._denied:
            return EntitlementVerdict(
                entitled=False, operation_type=operation_type,
                mode="denied",
                reason="mock: denied",
            )
        # Default → hold.
        ref = self._holds_issued.setdefault(key, f"MOCK-HOLD-{secrets.token_hex(4)}")
        return EntitlementVerdict(
            entitled=False, operation_type=operation_type,
            mode="hold", hold_ref=ref,
            reason="mock: no subscription",
        )


def get_default_provider() -> AbstractEntitlementProvider:
    from django.conf import settings
    name = getattr(settings, "BMW_ECU_ENTITLEMENT_PROVIDER", "default")
    if name == "default":
        return DefaultEntitlementProvider()
    if name == "mock":
        return MockEntitlementProvider()
    raise ValueError(f"Unknown entitlement provider: {name}")
