"""Gift credit service — promotional grants tried BEFORE wallet/Paymob.

The consumption path is atomic (SELECT FOR UPDATE on the active gift
row, decrement, audit-row). Counted credits decrement by 1 per use;
time-bounded subscription windows are touched (audit row only).

Used by:
  - settlement.GiftFirstSettlementProvider — at capture time.
  - entitlement.DefaultEntitlementProvider — at Coding entitlement
    verification time, treats an active gift as `mode="gift"` entitlement.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from asgiref.sync import sync_to_async

from ..logging_setup import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class GiftConsumption:
    consumed: bool
    gift_pk: Optional[int]
    grant_type: str
    remaining_after: Optional[int]  # None for subscription_window
    reason: str = ""


@sync_to_async
def has_active_gift(*, tenant_schema: str,
                    grant_types: tuple[str, ...]) -> Optional[int]:
    """Return PK of the first consumable gift of the given type(s), or None.

    Read-only — does NOT decrement. Use `consume_gift` for that.
    Runs in the SHARED schema so super-admin-granted gifts are visible
    regardless of which tenant context the caller is in.
    """
    from django.db.models import Q
    from django.utils import timezone
    from django_tenants.utils import schema_context
    from ..models import GiftCredit

    with schema_context("public"):
        candidates = (GiftCredit.objects
                      .filter(tenant_schema=tenant_schema,
                              status="active",
                              grant_type__in=grant_types)
                      .filter(Q(valid_until__isnull=True) |
                              Q(valid_until__gt=timezone.now())))
        for g in candidates.order_by("granted_at"):
            if g.is_consumable():
                return g.pk
    return None


@sync_to_async
def consume_gift(*, tenant_schema: str,
                 grant_types: tuple[str, ...],
                 vin: str, operation_type: str,
                 reference: str = "") -> GiftConsumption:
    """Atomically pull one credit (or stamp a subscription window).

    Returns GiftConsumption(consumed=False, ...) if no consumable gift
    matches — caller falls back to wallet/Paymob.
    """
    from django.db import transaction
    from django.db.models import Q
    from django.utils import timezone
    from django_tenants.utils import schema_context
    from ..models import GiftCredit, GiftCreditUsage

    with schema_context("public"):
        with transaction.atomic():
            candidates = (GiftCredit.objects
                          .select_for_update()
                          .filter(tenant_schema=tenant_schema,
                                  status="active",
                                  grant_type__in=grant_types)
                          .filter(Q(valid_until__isnull=True) |
                                  Q(valid_until__gt=timezone.now()))
                          .order_by("granted_at"))
            for g in candidates:
                if not g.is_consumable():
                    continue
                if g.grant_type == "subscription_window":
                    # Time-bounded — log usage, do not decrement.
                    GiftCreditUsage.objects.create(
                        gift=g, vin=vin, operation_type=operation_type,
                        reference=reference,
                    )
                    log.info("Gift consumed (subscription)", extra={
                        "tenant": tenant_schema, "gift_pk": g.pk, "vin": vin,
                    })
                    return GiftConsumption(
                        consumed=True, gift_pk=g.pk,
                        grant_type=g.grant_type, remaining_after=None,
                    )
                # Counted credit — decrement.
                if g.credits_remaining <= 0:
                    continue
                g.credits_remaining -= 1
                if g.credits_remaining == 0:
                    g.status = "consumed"
                g.save(update_fields=["credits_remaining", "status"])
                GiftCreditUsage.objects.create(
                    gift=g, vin=vin, operation_type=operation_type,
                    reference=reference,
                )
                log.info("Gift consumed (credit)", extra={
                    "tenant": tenant_schema, "gift_pk": g.pk, "vin": vin,
                    "remaining": g.credits_remaining,
                })
                return GiftConsumption(
                    consumed=True, gift_pk=g.pk,
                    grant_type=g.grant_type,
                    remaining_after=g.credits_remaining,
                )
    return GiftConsumption(
        consumed=False, gift_pk=None, grant_type="",
        remaining_after=None, reason="no consumable gift",
    )


# Grant-type bundles used by the entitlement + settlement layers.
GIFT_TYPES_FOR_ISN = ("isn_credits", "subscription_window")
GIFT_TYPES_FOR_CODING = ("coding_credits", "subscription_window")
