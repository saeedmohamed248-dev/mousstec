"""ApiOrchestrator — glue between HTTP requests and ExecutionStrategyManager.

Owns the lifecycle:
    1. authorize fee
    2. invoke manager
    3. on SUCCESS         → capture fee + record audit row
    4. on SUSPENDED       → keep auth, return next wizard step
    5. on FAILED_*        → release fee + record audit row
    6. on exception       → release fee + translate error

Kept thin and pure-async so the views are 20-line wrappers.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from asgiref.sync import sync_to_async

from ..execution.base import StrategyContext, StrategyOutcome, StrategyResult
from ..execution.manager import ExecutionStrategyManager
from ..logging_setup import get_logger
from ..services.billing_gate import AbstractBillingGate, AuthorizationResult
from ..services.chatbot_translator import (
    ChatbotPayload, translate_exception, translate_result,
)

log = get_logger(__name__)


@dataclass
class OrchestratedResponse:
    """What the view serialises to JSON."""
    chatbot: ChatbotPayload
    outcome: str
    session_id: Optional[int] = None
    wizard_session_id: Optional[int] = None
    billing: dict[str, Any] = None  # type: ignore[assignment]
    next_endpoint: str = ""

    def to_json(self) -> dict[str, Any]:
        body = self.chatbot.to_json()
        body.update(
            outcome=self.outcome,
            session_id=self.session_id,
            wizard_session_id=self.wizard_session_id,
            billing=self.billing or {},
            next_endpoint=self.next_endpoint,
        )
        return body


class ApiOrchestrator:
    def __init__(self, manager: ExecutionStrategyManager,
                 billing: AbstractBillingGate) -> None:
        self.manager = manager
        self.billing = billing

    # --- Entry: /api/ecu/execute -----------------------------------------
    async def execute(self, ctx: StrategyContext,
                      target_fem_ctx: Optional[StrategyContext] = None
                      ) -> OrchestratedResponse:
        # 1. Authorize.
        try:
            auth = await self.billing.authorize_diagnostic_fee(vin=ctx.vin)
        except Exception as e:
            return OrchestratedResponse(
                chatbot=translate_exception(e),
                outcome="billing_declined", billing=None,
            )

        # 2. Run.
        try:
            result = await self.manager.run_extract_then_inject(ctx, target_fem_ctx)
        except Exception as e:
            await self._safe_release(ctx.vin, reason=f"manager raised: {e}")
            payload = translate_exception(e)
            return OrchestratedResponse(
                chatbot=payload, outcome="error",
                billing=self._billing_dict(auth, captured=False, released=True),
            )

        # 3. Finalise based on outcome.
        return await self._finalise(ctx, result, auth)

    # --- Entry: /api/ecu/wizard/step -------------------------------------
    async def wizard_step(self, ctx: StrategyContext,
                          wizard_strategy, response) -> OrchestratedResponse:
        """`wizard_strategy` is the InteractiveGuidedStrategy instance.

        The session must already be authorized (created by /execute), so
        we look up the existing auth instead of opening a new one.
        """
        auth = await self._lookup_open_auth(ctx.vin)
        try:
            result = await wizard_strategy.handle_step(ctx, response)
        except Exception as e:
            if auth:
                await self._safe_release(ctx.vin, reason=f"wizard raised: {e}")
            payload = translate_exception(e)
            return OrchestratedResponse(
                chatbot=payload, outcome="error",
                wizard_session_id=ctx.wizard_session_id,
                billing=(self._billing_dict(auth, captured=False, released=True)
                         if auth else None),
            )
        return await self._finalise(ctx, result, auth)

    # --- Internals --------------------------------------------------------
    async def _finalise(self, ctx: StrategyContext, result: StrategyResult,
                        auth: Optional[AuthorizationResult]
                        ) -> OrchestratedResponse:
        next_endpoint = ""
        captured = released = False
        if result.outcome == StrategyOutcome.SUCCESS and auth is not None:
            try:
                await self.billing.capture_fee(vin=ctx.vin)
                captured = True
            except Exception as e:
                log.error("Capture failed AFTER successful run",
                          extra={"vin": ctx.vin, "err": str(e)})
        elif result.outcome in (StrategyOutcome.FAILED_ROLLED_BACK,
                               StrategyOutcome.FAILED_UNRECOVERABLE,
                               StrategyOutcome.PARTIAL) and auth is not None:
            await self._safe_release(ctx.vin,
                                     reason=result.error_message or result.error_code)
            released = True
        elif result.outcome == StrategyOutcome.SUSPENDED:
            next_endpoint = "/api/ecu/wizard/step"

        chatbot = translate_result(result)

        wizard_sid = None
        if result.wizard_next_step is not None:
            wizard_sid = result.wizard_next_step.get("session_id")

        return OrchestratedResponse(
            chatbot=chatbot,
            outcome=result.outcome.value,
            wizard_session_id=wizard_sid,
            next_endpoint=next_endpoint,
            billing=self._billing_dict(auth, captured=captured, released=released),
        )

    async def _lookup_open_auth(self, vin: str) -> Optional[AuthorizationResult]:
        @sync_to_async
        def _fetch() -> Optional[AuthorizationResult]:
            from ..models import DiagnosticFeeCharge
            row = (DiagnosticFeeCharge.objects
                   .filter(vin=vin, status="authorized").first())
            if row is None:
                return None
            return AuthorizationResult(
                authorization_ref=row.authorization_ref, vin=row.vin,
                amount=row.amount, currency=row.currency, status=row.status,
                is_new=False,
            )
        return await _fetch()

    async def _safe_release(self, vin: str, *, reason: str) -> None:
        try:
            await self.billing.release_fee(vin=vin, reason=reason)
        except Exception as e:
            log.warning("Release fee failed (best-effort)",
                        extra={"vin": vin, "err": str(e)})

    @staticmethod
    def _billing_dict(auth: Optional[AuthorizationResult],
                      *, captured: bool, released: bool) -> dict[str, Any]:
        if auth is None:
            return {}
        return {
            "authorized": str(auth.amount), "currency": auth.currency,
            "authorization_ref": auth.authorization_ref,
            "captured": captured, "released": released,
        }
