"""Financial settlement providers — invoked by LocalBillingGate.on_captured.

The capture in the bmw_ecu ledger (DiagnosticFeeCharge.status = 'captured')
is the *commitment*. Settlement is the **physical money movement**:
    A. Deduct from the workshop's wallet_balance on clients.Client, OR
    B. Generate a Paymob iframe so the workshop pays the 450 EGP card.

Three providers ship:

    WalletDeductProvider      — atomic UPDATE on Client.wallet_balance.
    PaymobInvoiceProvider     — delegates to billing.services.paymob.
    WalletThenPaymobProvider  — tries wallet, falls back to Paymob on
                                insufficient balance.

Selection is via the BMW_ECU_SETTLEMENT_PROVIDER setting; default is the
composite. Every settlement attempt creates a BmwEcuSettlement audit row
so accounting + support can trace the money.

Provider failures NEVER raise out of on_captured — the diagnostic capture
has already succeeded and the technician finished the job. Settlement
failures are logged + audit-rowed for the support team to chase.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from asgiref.sync import sync_to_async

from ..exceptions import BillingError
from ..logging_setup import get_logger

log = get_logger(__name__)


@dataclass
class SettlementOutcome:
    mode: str                    # "gift" | "wallet" | "paymob" | "failed"
    succeeded: bool
    amount: Decimal
    wallet_before: Optional[Decimal] = None
    wallet_after: Optional[Decimal] = None
    paymob_iframe_url: str = ""
    gift_pk: Optional[int] = None
    gift_grant_type: str = ""
    gift_remaining_after: Optional[int] = None
    error_message: str = ""


class AbstractSettlementProvider(abc.ABC):
    """One provider per settlement strategy."""

    name: str

    @abc.abstractmethod
    async def settle(self, *, authorization_ref: str, vin: str,
                     amount: Decimal, currency: str,
                     tenant_schema: Optional[str] = None) -> SettlementOutcome: ...


# ---------------------------------------------------------------------------
# Provider A — wallet deduct
# ---------------------------------------------------------------------------
class WalletDeductProvider(AbstractSettlementProvider):
    """Atomically deducts the fee from the active tenant's Client.wallet_balance.

    Uses SELECT FOR UPDATE inside a transaction so concurrent captures on
    the same tenant can't race past the balance check.
    """
    name = "wallet"

    async def settle(self, *, authorization_ref: str, vin: str,
                     amount: Decimal, currency: str,
                     tenant_schema: Optional[str] = None) -> SettlementOutcome:
        if currency != "EGP":
            return SettlementOutcome(
                mode="failed", succeeded=False, amount=amount,
                error_message=f"wallet supports EGP only, got {currency}",
            )
        try:
            before, after = await self._deduct(tenant_schema, amount)
        except _InsufficientBalance as e:
            log.warning("Wallet insufficient", extra={
                "vin": vin, "needed": str(amount), "have": str(e.have),
            })
            return SettlementOutcome(
                mode="wallet", succeeded=False, amount=amount,
                wallet_before=e.have, wallet_after=e.have,
                error_message=f"insufficient balance ({e.have} EGP)",
            )
        log.info("Wallet settled", extra={
            "vin": vin, "before": str(before), "after": str(after),
        })
        return SettlementOutcome(
            mode="wallet", succeeded=True, amount=amount,
            wallet_before=before, wallet_after=after,
        )

    @staticmethod
    @sync_to_async
    def _deduct(tenant_schema: Optional[str],
                amount: Decimal) -> tuple[Decimal, Decimal]:
        from django.db import transaction
        from django.db.models import F
        from clients.models import Client

        schema = tenant_schema or _current_schema()
        if not schema:
            raise BillingError("settlement: no active tenant schema")

        with transaction.atomic():
            client = (Client.objects.select_for_update()
                      .filter(schema_name=schema).first())
            if client is None:
                raise BillingError(f"settlement: tenant {schema!r} not found")
            before: Decimal = client.wallet_balance
            if before < amount:
                raise _InsufficientBalance(have=before)
            Client.objects.filter(pk=client.pk).update(
                wallet_balance=F("wallet_balance") - amount,
            )
            client.refresh_from_db(fields=["wallet_balance"])
            return before, client.wallet_balance


class _InsufficientBalance(Exception):
    def __init__(self, have: Decimal) -> None:
        self.have = have


# ---------------------------------------------------------------------------
# Provider B — Paymob iframe
# ---------------------------------------------------------------------------
class PaymobInvoiceProvider(AbstractSettlementProvider):
    """Generates a Paymob iframe URL the workshop opens to pay the 450 EGP.

    Real card capture happens asynchronously — the workshop sees the
    iframe in the chatbot, pays, and the existing
    `payment/paymob/callback/` view marks settlement complete via HMAC.

    On success we hand back the iframe URL; the chatbot surfaces it in
    `visual_aid_url` / `required_action="open_payment_iframe"`.
    """
    name = "paymob"

    async def settle(self, *, authorization_ref: str, vin: str,
                     amount: Decimal, currency: str,
                     tenant_schema: Optional[str] = None) -> SettlementOutcome:
        try:
            iframe_url = await self._build_iframe(authorization_ref, vin, amount)
        except Exception as e:
            log.error("Paymob iframe build failed", extra={
                "vin": vin, "err": str(e),
            })
            return SettlementOutcome(
                mode="failed", succeeded=False, amount=amount,
                error_message=f"paymob: {e}",
            )
        return SettlementOutcome(
            mode="paymob", succeeded=True, amount=amount,
            paymob_iframe_url=iframe_url,
        )

    @staticmethod
    @sync_to_async
    def _build_iframe(ref: str, vin: str, amount: Decimal) -> str:
        from billing.services.paymob import create_iframe_url
        return create_iframe_url(
            amount_egp=amount,
            order_ref=f"bmwecu-{ref}",
            item_name=f"BMW ECU unlock — VIN {vin}",
            metadata={"source": "bmw_ecu", "vin": vin,
                      "authorization_ref": ref},
            cache_key_prefix="paymob_bmwecu",
        )


# ---------------------------------------------------------------------------
# Provider C — composite (wallet first, paymob fallback)
# ---------------------------------------------------------------------------
class WalletThenPaymobProvider(AbstractSettlementProvider):
    """Production default. Try wallet, fall back to Paymob iframe."""
    name = "wallet_then_paymob"

    def __init__(self) -> None:
        self.wallet = WalletDeductProvider()
        self.paymob = PaymobInvoiceProvider()

    async def settle(self, *, authorization_ref: str, vin: str,
                     amount: Decimal, currency: str,
                     tenant_schema: Optional[str] = None) -> SettlementOutcome:
        outcome = await self.wallet.settle(
            authorization_ref=authorization_ref, vin=vin,
            amount=amount, currency=currency, tenant_schema=tenant_schema,
        )
        if outcome.succeeded:
            return outcome
        log.info("Wallet path failed, falling back to Paymob",
                 extra={"vin": vin, "reason": outcome.error_message})
        paymob_outcome = await self.paymob.settle(
            authorization_ref=authorization_ref, vin=vin,
            amount=amount, currency=currency, tenant_schema=tenant_schema,
        )
        # Preserve the wallet snapshot so accounting can see "we tried wallet,
        # had X EGP, fell back to paymob".
        paymob_outcome.wallet_before = outcome.wallet_before
        paymob_outcome.wallet_after = outcome.wallet_after
        return paymob_outcome


# ---------------------------------------------------------------------------
# Provider D — gift first (try a promotional credit before any money moves)
# ---------------------------------------------------------------------------
class GiftFirstProvider(AbstractSettlementProvider):
    """Production default. Consume one gift credit if available, otherwise
    fall through to the (configurable) money-movement provider chain.

    Always uses the same `(tenant_schema, vin)` pair as the consumption
    key so retries are idempotent at the gift_credits.consume_gift layer.
    """
    name = "gift_then_money"

    def __init__(self,
                 money_provider: Optional[AbstractSettlementProvider] = None
                 ) -> None:
        self.money = money_provider or WalletThenPaymobProvider()

    async def settle(self, *, authorization_ref: str, vin: str,
                     amount: Decimal, currency: str,
                     tenant_schema: Optional[str] = None) -> SettlementOutcome:
        from .gift_credits import GIFT_TYPES_FOR_ISN, consume_gift

        schema = tenant_schema or await _current_schema_async()
        if schema:
            # Try gift first. Use ISN bundle by default — coding goes
            # through the entitlement gate, not settlement.
            result = await consume_gift(
                tenant_schema=schema,
                grant_types=GIFT_TYPES_FOR_ISN,
                vin=vin, operation_type="isn",
                reference=authorization_ref,
            )
            if result.consumed:
                log.info("Settled via gift", extra={
                    "vin": vin, "gift_pk": result.gift_pk,
                    "remaining": result.remaining_after,
                })
                return SettlementOutcome(
                    mode="gift", succeeded=True, amount=amount,
                    gift_pk=result.gift_pk,
                    gift_grant_type=result.grant_type,
                    gift_remaining_after=result.remaining_after,
                )

        return await self.money.settle(
            authorization_ref=authorization_ref, vin=vin,
            amount=amount, currency=currency, tenant_schema=tenant_schema,
        )


async def _current_schema_async() -> Optional[str]:
    from asgiref.sync import sync_to_async
    return await sync_to_async(_current_schema)()


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------
def get_default_provider() -> AbstractSettlementProvider:
    """Resolve from Django settings; default to gift_then_wallet_then_paymob."""
    from django.conf import settings
    name = getattr(settings, "BMW_ECU_SETTLEMENT_PROVIDER",
                   "gift_then_wallet_then_paymob")
    if name == "wallet":
        return WalletDeductProvider()
    if name == "paymob":
        return PaymobInvoiceProvider()
    if name == "wallet_then_paymob":
        return WalletThenPaymobProvider()
    # Default — gift gets first crack.
    return GiftFirstProvider(money_provider=WalletThenPaymobProvider())


def _current_schema() -> Optional[str]:
    from django.db import connection
    tenant = getattr(connection, "tenant", None)
    return getattr(tenant, "schema_name", None) if tenant else None
