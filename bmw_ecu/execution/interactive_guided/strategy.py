"""InteractiveGuidedStrategy — technician wizard.

This strategy uniquely returns `StrategyOutcome.SUSPENDED` between steps.
The frontend drives the loop: every POST advances the state machine by
one transition, persists, and yields the next WizardStep.
"""
from __future__ import annotations

from typing import Optional

from ...exceptions import IsnMismatch
from ...isn.extractor import ISN_LENGTH
from ...logging_setup import get_logger
from ...uds.client import UdsClient
from ...uds.services import DiagSession
from ..base import ExecutionStrategy, StrategyContext, StrategyOutcome, StrategyResult
from .frontend_contract import WizardStep, WizardStepKind, WizardResponse
from .pinout_repository import PinoutRepository
from . import session_resume
from .state_machine import WizardState, WizardStateMachine

log = get_logger(__name__)


class InteractiveGuidedStrategy(ExecutionStrategy):
    name = "interactive_guided"
    requires_technician = True

    def __init__(self, pinouts: Optional[PinoutRepository] = None) -> None:
        self.pinouts = pinouts or PinoutRepository()

    # --- Public entry points (Manager-facing) -----------------------------
    async def extract_isn(self, ctx: StrategyContext) -> StrategyResult:
        sm = WizardStateMachine()
        sm.data.vin = ctx.vin
        sm.data.ecu_name = ctx.profile.name
        return await self._yield_next(ctx, sm)

    async def inject_isn(self, ctx: StrategyContext) -> StrategyResult:
        # If a wizard session is already mid-flight, resume it. Otherwise
        # this is a fresh inject-only flow (target_isn provided directly).
        if ctx.wizard_session_id is not None:
            sm = await session_resume.load_session(ctx.wizard_session_id)
        else:
            sm = WizardStateMachine()
            sm.data.vin = ctx.vin
            sm.data.ecu_name = ctx.profile.name
            sm.data.captured_isn = ctx.target_isn
            sm.advance(WizardState.SHOWING_PINOUT)
        return await self._yield_next(ctx, sm)

    async def rollback(self, ctx: StrategyContext, *, reason: str) -> StrategyResult:
        if ctx.wizard_session_id is None:
            return self._failed(reason=reason)
        sm = await session_resume.load_session(ctx.wizard_session_id)
        sm.fail(reason)
        await session_resume.save_session(
            session_id=ctx.wizard_session_id, sm=sm,
            vin=ctx.vin, ecu_name=ctx.profile.name,
        )
        return self._failed(reason=reason)

    # --- External call from REST endpoint ---------------------------------
    async def handle_step(self, ctx: StrategyContext,
                          response: WizardResponse) -> StrategyResult:
        """Called by /api/wizard/step. Applies one technician input."""
        assert ctx.wizard_session_id is not None
        sm = await session_resume.load_session(ctx.wizard_session_id)

        if not response.confirmed and response.step_kind != WizardStepKind.CAPTURE_ISN:
            sm.fail("technician declined")
            await self._persist(ctx, sm)
            return self._failed("declined by technician")

        try:
            if sm.state == WizardState.SHOWING_PINOUT:
                sm.advance(WizardState.AWAITING_POWER)
            elif sm.state == WizardState.AWAITING_POWER:
                sm.advance(WizardState.AWAITING_GLITCH)
            elif sm.state == WizardState.AWAITING_GLITCH:
                sm.advance(WizardState.AWAITING_ISN)
            elif sm.state == WizardState.AWAITING_ISN:
                if not response.isn_hex or len(response.isn_hex) != ISN_LENGTH * 2:
                    sm.fail("isn_hex missing or wrong length")
                else:
                    try:
                        sm.data.captured_isn = bytes.fromhex(response.isn_hex)
                        sm.advance(WizardState.INJECTING)
                    except ValueError:
                        sm.fail("isn_hex not valid hex")
        except Exception as e:
            sm.fail(str(e))

        await self._persist(ctx, sm)

        # If we just hit INJECTING, perform the injection inline and finalise.
        if sm.state == WizardState.INJECTING and sm.data.captured_isn is not None:
            try:
                await self._do_inject(ctx, sm.data.captured_isn)
                sm.advance(WizardState.DONE)
            except Exception as e:
                sm.fail(f"injection failed: {e}")
            await self._persist(ctx, sm)

        return await self._yield_next(ctx, sm)

    # --- Internals --------------------------------------------------------
    async def _yield_next(self, ctx: StrategyContext,
                          sm: WizardStateMachine) -> StrategyResult:
        # Persist whatever transition just happened.
        sid = await self._persist(ctx, sm)

        if sm.state == WizardState.DONE:
            return StrategyResult(
                outcome=StrategyOutcome.SUCCESS, strategy_name=self.name,
                isn=sm.data.captured_isn,
            )
        if sm.state == WizardState.FAILED:
            return self._failed(reason=sm.data.error_code or "wizard failed")

        # Build the next step for the frontend.
        step = await self._build_step(ctx, sm)
        return StrategyResult(
            outcome=StrategyOutcome.SUSPENDED,
            strategy_name=self.name,
            wizard_next_step={"session_id": sid, "step": step.to_json()},
        )

    async def _build_step(self, ctx: StrategyContext,
                          sm: WizardStateMachine) -> WizardStep:
        diagram = await self.pinouts.get(ctx.profile.name)
        diagram_url = diagram.image_url if diagram else None
        callouts = diagram.callouts if diagram else []

        if sm.state in (WizardState.INIT, WizardState.SHOWING_PINOUT):
            if sm.state == WizardState.INIT:
                sm.advance(WizardState.SHOWING_PINOUT)
            return WizardStep(
                kind=WizardStepKind.SHOW_PINOUT,
                title=f"Pinout — {ctx.profile.name}",
                instructions="راجع الـ pinout وتأكد إنك مستعد قبل ما تبدأ.",
                pinout_diagram_url=diagram_url, pinout_callouts=callouts,
            )
        if sm.state == WizardState.AWAITING_POWER:
            return WizardStep(
                kind=WizardStepKind.CONFIRM_POWER,
                title="Power up the ECU on the bench",
                instructions="وصل 12V + GND حسب الـ pinout. اضغط تأكيد لما يكون شغال.",
            )
        if sm.state == WizardState.AWAITING_GLITCH:
            return WizardStep(
                kind=WizardStepKind.CONFIRM_GLITCH,
                title="Ground BOOT pin",
                instructions="وصل الـ BOOT pin بالـ GND، ثم confirm. هنبدأ القراءة بعد كده.",
            )
        if sm.state == WizardState.AWAITING_ISN:
            return WizardStep(
                kind=WizardStepKind.CAPTURE_ISN,
                title="Capture ISN from BDM probe",
                instructions="استخدم الـ Xprog/Trasdata لقراءة الـ ISN 32 byte والصقها هنا.",
                input_schema={"type": "object",
                              "properties": {"isn_hex": {"type": "string",
                                                         "pattern": "^[0-9a-fA-F]{64}$"}},
                              "required": ["isn_hex"]},
            )
        if sm.state == WizardState.INJECTING:
            return WizardStep(
                kind=WizardStepKind.CONFIRM_INJECTION,
                title="Ready to inject into FEM",
                instructions="ركّب الـ FEM في السيارة وقفل OBD. هنحقن الـ ISN عبر UDS.",
            )
        return WizardStep(kind=WizardStepKind.ERROR,
                          title="Unknown state",
                          instructions=f"state={sm.state.value}")

    async def _do_inject(self, ctx: StrategyContext, isn: bytes) -> None:
        client = UdsClient(ctx.transport, ecu_addr=ctx.profile.uds_isn_did >> 8,
                           session_name="wizard_inject")
        await client.diagnostic_session_control(DiagSession.PROGRAMMING)
        await ctx.security.unlock(vin=ctx.vin)

        async def dump() -> bytes:
            try:
                return await client.read_data_by_identifier(ctx.profile.uds_isn_did)
            except Exception:
                return bytes(ISN_LENGTH)

        await ctx.preflight.check(
            vin=ctx.vin, ecu_name=ctx.profile.name, memory_region="EEPROM",
            dump_callable=dump, write_kind="coding",
        )
        await client.write_data_by_identifier(ctx.profile.uds_isn_did, isn)
        readback = await client.read_data_by_identifier(ctx.profile.uds_isn_did)
        if readback != isn:
            raise IsnMismatch("wizard read-back mismatch")

    async def _persist(self, ctx: StrategyContext, sm: WizardStateMachine) -> int:
        sid = await session_resume.save_session(
            session_id=ctx.wizard_session_id, sm=sm,
            vin=ctx.vin, ecu_name=ctx.profile.name,
        )
        ctx.wizard_session_id = sid
        return sid

    @staticmethod
    def _failed(reason: str) -> StrategyResult:
        return StrategyResult(
            outcome=StrategyOutcome.FAILED_ROLLED_BACK,
            strategy_name="interactive_guided",
            error_code="WIZARD_FAILED", error_message=reason,
        )
