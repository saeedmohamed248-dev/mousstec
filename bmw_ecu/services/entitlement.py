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
    mode: str                    # "subscription" | "hold" | "denied"
    subscription_ref: str = ""   # provider-specific id (TenantSubscription pk, etc.)
    hold_ref: str = ""           # idempotency key when mode="hold"
    reason: str = ""


class AbstractEntitlementProvider(abc.ABC):
    """One provider per environment. All implementations MUST be idempotent
    against (vin, operation_type) — chatbot retries are safe."""

    @abc.abstractmethod
    async def verify(self, *, vin: str,
                     operation_type: OperationType,
                     tenant_schema: Optional[str] = None
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
                     tenant_schema: Optional[str] = None
                     ) -> EntitlementVerdict:
        if operation_type == OperationType.ISN:
            # ISN is always entitled at this layer — the fee gate handles it.
            return EntitlementVerdict(
                entitled=True, operation_type=operation_type,
                mode="subscription", reason="ISN flow uses pay-per-success",
            )

        # Coding path.
        from django.conf import settings

        global_on = bool(getattr(settings, "BMW_ECU_CODING_ENTITLED_GLOBALLY",
                                 False))
        whitelist = set(getattr(settings, "BMW_ECU_CODING_ENTITLED_TENANTS",
                                set()))
        schema = tenant_schema or await _current_schema()

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
                     tenant_schema: Optional[str] = None
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
