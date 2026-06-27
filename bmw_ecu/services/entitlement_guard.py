"""Thin entitlement adapter for the chatbot-guided service orchestrators.

The granular billing layer (sub-commit 1) exposes two primitives:

  _check_granular_grant_sync(tenant_schema, feature_code, operation_type)
      → EntitlementVerdict | None

  consume_feature_usage_sync(verdict, tenant_schema, vin, operation_ref)
      → bool

Each premium orchestrator (bench_orchestrator, frm_recovery, egs_isn_reset,
acsm_crash_reset, cbs_battery_manager) needs the SAME two calls at the
SAME two life-cycle points:
  • call check() right after the technician fires the first event, so
    an unentitled session never advances past IDLE;
  • call consume() exactly once on FINISH, AFTER the work has succeeded
    on the bench, so the grant only counts a USED session.

This adapter bundles that pattern. Production code constructs the guard
with the real feature_code + tenant_schema; tests inject
MockEntitlementGuard so the orchestrator logic can be verified without
touching the DB.

Why a separate file
-------------------
The five orchestrators each live in a different sub-package
(key_learning, legacy, premium). Putting the adapter here in
`services/` makes it the single import they all reach for — and keeps
the orchestrators free of any DB-coupled code (they stay pure-Python
state machines, mock-driven in tests).
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Optional

from .entitlement import (
    EntitlementVerdict,
    OperationType,
    _check_granular_grant_sync,
    consume_feature_usage_sync,
)


class AbstractEntitlementGuard(abc.ABC):
    """The shape every orchestrator's `entitlement=...` slot expects.

    Two methods, both synchronous (the orchestrators run inside an
    asyncio loop but the guards' DB work is wrapped by the caller via
    sync_to_async at the integration boundary, not here)."""

    feature_code: str

    @abc.abstractmethod
    def check(self) -> tuple[bool, str]:
        """Verify the tenant has an active grant. Returns
        (entitled, reason). On the entitled branch the verdict is
        cached so a follow-up consume() doesn't re-query."""

    @abc.abstractmethod
    def consume(self, *, vin: str = "",
                operation_ref: str = "") -> bool:
        """Decrement the grant's usage counter + audit-log the use.
        Returns False when no verdict was cached (check() wasn't
        called) or when the underlying consume layer rejects the
        write. Callers ALWAYS continue regardless — the work was done."""


# ─────────────────────────────────────────────────────────────────────
# Production guard — hits the real granular billing tables.
# ─────────────────────────────────────────────────────────────────────
@dataclass
class EntitlementGuard(AbstractEntitlementGuard):
    """Production guard backed by _check_granular_grant_sync +
    consume_feature_usage_sync from `entitlement.py`.

    Callers MUST be on the tenant schema by the time check() runs — the
    underlying sync layer queries Feature / TenantPackageGrant /
    TenantFeatureGrant tables which live in the tenant schema."""

    feature_code: str
    tenant_schema: str
    operation_type: OperationType = OperationType.CODING
    _verdict: Optional[EntitlementVerdict] = field(default=None, init=False)

    @property
    def verdict(self) -> Optional[EntitlementVerdict]:
        return self._verdict

    def check(self) -> tuple[bool, str]:
        if not self.tenant_schema:
            return False, "tenant_schema unavailable on the current request"
        verdict = _check_granular_grant_sync(
            tenant_schema=self.tenant_schema,
            feature_code=self.feature_code,
            operation_type=self.operation_type,
        )
        if verdict is None:
            # No grant for this tenant + feature at all.
            return False, (
                f"no active grant for feature {self.feature_code!r}. "
                f"اشترك من /bmw-ecu/storefront/ قبل تشغيل الـ service."
            )
        if not verdict.entitled:
            return False, (
                verdict.reason
                or f"grant for {self.feature_code!r} is not active"
            )
        self._verdict = verdict
        return True, "entitled"

    def consume(self, *, vin: str = "",
                operation_ref: str = "") -> bool:
        if self._verdict is None or not self._verdict.entitled:
            return False
        try:
            return consume_feature_usage_sync(
                verdict=self._verdict,
                tenant_schema=self.tenant_schema,
                vin=vin,
                operation_ref=operation_ref,
            )
        except Exception:           # pragma: no cover
            # consume is best-effort — the work already succeeded.
            return False


# ─────────────────────────────────────────────────────────────────────
# Test double — pure Python, no DB.
# ─────────────────────────────────────────────────────────────────────
@dataclass
class MockEntitlementGuard(AbstractEntitlementGuard):
    """Test double. Configure `entitled_result` to flip the check()
    response; orchestrator tests assert on `check_calls` and
    `consume_calls` to verify the integration wiring is correct."""

    feature_code: str = "mock_feature"
    entitled_result: bool = True
    refusal_reason: str = "mock: not entitled"

    check_calls: int = 0
    consume_calls: list[dict] = field(default_factory=list)

    def check(self) -> tuple[bool, str]:
        self.check_calls += 1
        if self.entitled_result:
            return True, "entitled (mock)"
        return False, self.refusal_reason

    def consume(self, *, vin: str = "",
                operation_ref: str = "") -> bool:
        self.consume_calls.append({"vin": vin, "operation_ref": operation_ref})
        return True
