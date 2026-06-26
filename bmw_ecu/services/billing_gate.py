"""Pay-Per-Success billing gate.

Business rule (frozen by product):
    450 EGP per successfully unlocked/synced VIN. Authorized at session
    start, captured only on terminal SUCCESS. Failures release the hold —
    technician walks away with zero charge.

Idempotency: the gate keys every authorization by VIN. Re-calling
authorize() while an open auth exists for the same VIN returns the
existing row (no duplicate hold). The API layer can replay safely if the
chatbot retries.

This module is the contract. The default `LocalBillingGate` persists to
the bmw_ecu DiagnosticFeeCharge ledger. Production deploys can subclass
to delegate to the wider Mousstec billing app (Paymob, manual receipts,
treasury) — see hooks at the bottom.
"""
from __future__ import annotations

import abc
import secrets
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from asgiref.sync import sync_to_async

from ..exceptions import BillingError, FeeAuthorizationDeclined
from ..logging_setup import get_logger

log = get_logger(__name__)

DEFAULT_FEE_EGP: Decimal = Decimal("450.00")


@dataclass(frozen=True)
class AuthorizationResult:
    authorization_ref: str
    vin: str
    amount: Decimal
    currency: str
    status: str
    is_new: bool                    # False = idempotent replay
    operation_type: str = "isn"     # "isn" | "coding"


class AbstractBillingGate(abc.ABC):
    """Three lifecycle calls — authorize / capture / release. Idempotent."""

    @abc.abstractmethod
    async def authorize_diagnostic_fee(
        self, *, vin: str, amount: Decimal = DEFAULT_FEE_EGP,
        currency: str = "EGP", session_id: Optional[int] = None,
    ) -> AuthorizationResult: ...

    @abc.abstractmethod
    async def capture_fee(self, *, vin: str) -> AuthorizationResult: ...

    @abc.abstractmethod
    async def release_fee(self, *, vin: str, reason: str = "") -> AuthorizationResult: ...

    # --- Generic entitlement check (Coding + future op types) -------------
    async def verify_coding_subscription_or_hold(
        self, *, vin: str, operation_type: str = "coding",
        feature_code: Optional[str] = None,
    ) -> "CodingEntitlement":
        """Verify the workshop is entitled to run `operation_type` (and
        optionally a specific granular `feature_code`).

        Returns a CodingEntitlement (entitled, mode, refs). Default impl
        delegates to the configured EntitlementProvider — ISN bypasses
        the subscription check and always returns entitled=True with
        mode='subscription' (the fee gate handles ISN billing instead).

        When `feature_code` is provided, the provider consults the
        TenantPackageGrant + TenantFeatureGrant tables first; legacy
        settings-whitelist / gift behaviour kicks in as a fallback so
        the existing 70 entitlement tests stay green.
        """
        from .entitlement import (
            OperationType, get_default_provider, EntitlementVerdict,
        )

        try:
            op = OperationType(operation_type)
        except ValueError as e:
            raise BillingError(f"Unknown operation_type: {operation_type}") from e

        provider = self._entitlement_provider() or get_default_provider()
        verdict: EntitlementVerdict = await provider.verify(
            vin=vin, operation_type=op, feature_code=feature_code,
        )
        return CodingEntitlement(
            entitled=verdict.entitled,
            operation_type=verdict.operation_type.value,
            mode=verdict.mode,
            subscription_ref=verdict.subscription_ref,
            hold_ref=verdict.hold_ref,
            reason=verdict.reason,
            feature_code=verdict.feature_code,
            grant_kind=verdict.grant_kind,
            grant_pk=verdict.grant_pk,
            usage_remaining=verdict.usage_remaining,
        )

    def _entitlement_provider(self):
        """Override in subclass to inject a stub provider in tests."""
        return None


@dataclass(frozen=True)
class CodingEntitlement:
    """Outcome of an entitlement check — chatbot-friendly shape."""
    entitled: bool
    operation_type: str
    mode: str                       # "subscription" | "hold" | "denied" | "package" | "feature_grant" | "gift"
    subscription_ref: str = ""
    hold_ref: str = ""
    reason: str = ""
    # ── Granular-grant fields (populated when verify was called with feature_code)
    feature_code: str = ""
    grant_kind: str = ""            # "package" | "feature" — empty for legacy paths
    grant_pk: int = 0               # row id; consumers pass this to consume_feature_usage()
    usage_remaining: Optional[int] = None  # None when unlimited


# ---------------------------------------------------------------------------
# Concrete: Django-ORM-backed gate writing to the DiagnosticFeeCharge ledger.
# ---------------------------------------------------------------------------
class LocalBillingGate(AbstractBillingGate):
    """Default gate. Persists to the bmw_ecu ledger inside the tenant DB."""

    async def authorize_diagnostic_fee(
        self, *, vin: str, amount: Decimal = DEFAULT_FEE_EGP,
        currency: str = "EGP", session_id: Optional[int] = None,
    ) -> AuthorizationResult:
        result = await self._authorize_sync(vin, amount, currency, session_id)
        log.info("Fee authorized", extra={
            "vin": vin, "amount": str(amount), "ref": result.authorization_ref,
            "replay": not result.is_new,
        })
        return result

    async def capture_fee(self, *, vin: str) -> AuthorizationResult:
        result = await self._finalise_sync(vin, target="captured")
        # Settlement is fire-and-record. Failures here do NOT roll back the
        # capture — the technician already finished the job. The audit row
        # tells accounting what's pending.
        try:
            await self.on_captured(result.authorization_ref, result.amount)
        except Exception as e:
            log.error("on_captured raised (suppressed)", extra={
                "vin": vin, "ref": result.authorization_ref, "err": str(e),
            })
        return result

    async def release_fee(self, *, vin: str, reason: str = "") -> AuthorizationResult:
        return await self._finalise_sync(vin, target="released", reason=reason)

    # --- sync DB helpers wrapped for asyncio ------------------------------
    @staticmethod
    @sync_to_async
    def _authorize_sync(vin: str, amount: Decimal, currency: str,
                        session_id: Optional[int]) -> AuthorizationResult:
        from django.db import transaction
        from ..models import DiagnosticFeeCharge

        with transaction.atomic():
            # Idempotent: if there's an open auth, return it.
            existing = (DiagnosticFeeCharge.objects
                        .select_for_update()
                        .filter(vin=vin, status="authorized")
                        .first())
            if existing is not None:
                return AuthorizationResult(
                    authorization_ref=existing.authorization_ref,
                    vin=existing.vin, amount=existing.amount,
                    currency=existing.currency, status=existing.status,
                    is_new=False,
                )
            ref = f"BMWECU-{secrets.token_urlsafe(12)}"
            row = DiagnosticFeeCharge.objects.create(
                vin=vin, amount=amount, currency=currency,
                status="authorized", authorization_ref=ref,
                session_id=session_id,
            )
            return AuthorizationResult(
                authorization_ref=row.authorization_ref, vin=row.vin,
                amount=row.amount, currency=row.currency, status=row.status,
                is_new=True,
            )

    @staticmethod
    @sync_to_async
    def _finalise_sync(vin: str, *, target: str,
                       reason: str = "") -> AuthorizationResult:
        from django.db import transaction
        from django.utils import timezone
        from ..models import DiagnosticFeeCharge

        with transaction.atomic():
            row = (DiagnosticFeeCharge.objects
                   .select_for_update()
                   .filter(vin=vin, status="authorized")
                   .first())
            if row is None:
                # No open auth — could be a replay after success. Return last
                # finalised row in target state so the caller is unsurprised.
                last = (DiagnosticFeeCharge.objects
                        .filter(vin=vin, status=target).order_by("-finalised_at")
                        .first())
                if last is None:
                    raise BillingError(
                        f"No open authorization for VIN {vin} to {target}",
                    )
                return AuthorizationResult(
                    authorization_ref=last.authorization_ref, vin=last.vin,
                    amount=last.amount, currency=last.currency,
                    status=last.status, is_new=False,
                )
            row.status = target
            row.finalised_at = timezone.now()
            if reason:
                row.error_message = reason
            row.save(update_fields=["status", "finalised_at", "error_message"])
            return AuthorizationResult(
                authorization_ref=row.authorization_ref, vin=row.vin,
                amount=row.amount, currency=row.currency, status=row.status,
                is_new=True,
            )

    # --- ERP integration hooks --------------------------------------------
    async def on_captured(self, ref: str, amount: Decimal) -> None:
        """Invoke the configured settlement provider and persist its audit.

        Default = WalletThenPaymobProvider (wallet deduct first, then
        Paymob iframe fallback). Override `provider_factory()` or set
        BMW_ECU_SETTLEMENT_PROVIDER in Django settings.
        """
        from .settlement import get_default_provider

        provider = self.provider_factory() or get_default_provider()
        charge = await self._load_charge_by_ref(ref)
        if charge is None:
            log.warning("on_captured: charge not found", extra={"ref": ref})
            return
        outcome = await provider.settle(
            authorization_ref=ref, vin=charge["vin"], amount=amount,
            currency=charge["currency"],
        )
        await self._save_settlement(charge_pk=charge["pk"], outcome=outcome)

    async def on_released(self, ref: str, amount: Decimal) -> None:
        """Override to release a Paymob auth hold or void a manual receipt.

        Default no-op: wallet deduction was never made, so nothing to refund.
        """

    def provider_factory(self):
        """Subclass override hook — return a SettlementProvider instance."""
        return None

    @staticmethod
    @sync_to_async
    def _load_charge_by_ref(ref: str):
        from ..models import DiagnosticFeeCharge
        row = DiagnosticFeeCharge.objects.filter(
            authorization_ref=ref).values("pk", "vin", "currency").first()
        return row

    @staticmethod
    @sync_to_async
    def _save_settlement(*, charge_pk: int, outcome) -> None:
        from django.db import transaction
        from ..models import BmwEcuSettlement
        with transaction.atomic():
            BmwEcuSettlement.objects.update_or_create(
                charge_id=charge_pk,
                defaults=dict(
                    mode=outcome.mode, succeeded=outcome.succeeded,
                    amount=outcome.amount, currency="EGP",
                    wallet_before=outcome.wallet_before,
                    wallet_after=outcome.wallet_after,
                    paymob_iframe_url=outcome.paymob_iframe_url,
                    gift_id=outcome.gift_pk,
                    error_message=outcome.error_message,
                ),
            )


# ---------------------------------------------------------------------------
# Test double — in-memory, no DB.
# ---------------------------------------------------------------------------
class MockBillingGate(AbstractBillingGate):
    """No-DB gate for unit tests. Tracks state in a dict."""

    def __init__(self, *, decline_vins: Optional[set[str]] = None) -> None:
        self._rows: dict[str, dict] = {}
        self._decline = decline_vins or set()

    async def authorize_diagnostic_fee(
        self, *, vin: str, amount: Decimal = DEFAULT_FEE_EGP,
        currency: str = "EGP", session_id: Optional[int] = None,
    ) -> AuthorizationResult:
        if vin in self._decline:
            raise FeeAuthorizationDeclined(f"VIN {vin} on decline list")
        if vin in self._rows and self._rows[vin]["status"] == "authorized":
            r = self._rows[vin]
            return AuthorizationResult(r["ref"], vin, r["amount"], r["currency"],
                                       "authorized", is_new=False)
        ref = f"MOCK-{secrets.token_hex(6)}"
        self._rows[vin] = {"ref": ref, "amount": amount, "currency": currency,
                           "status": "authorized"}
        return AuthorizationResult(ref, vin, amount, currency, "authorized",
                                   is_new=True)

    async def capture_fee(self, *, vin: str) -> AuthorizationResult:
        return self._finalise(vin, "captured")

    async def release_fee(self, *, vin: str, reason: str = "") -> AuthorizationResult:
        return self._finalise(vin, "released")

    def _finalise(self, vin: str, target: str) -> AuthorizationResult:
        if vin not in self._rows:
            raise BillingError(f"No row for {vin}")
        r = self._rows[vin]
        r["status"] = target
        return AuthorizationResult(r["ref"], vin, r["amount"], r["currency"],
                                   target, is_new=True)
