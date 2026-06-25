"""Persist + resume wizard sessions across requests.

The wizard suspends after every step. The Manager returns a SUSPENDED
StrategyResult with a `wizard_session_id`. The frontend posts the next
WizardResponse to /api/wizard/step with that ID, the backend loads the
state machine from DB, applies the response, and either yields the next
step or completes the injection.
"""
from __future__ import annotations

from typing import Optional

from asgiref.sync import sync_to_async

from .state_machine import WizardData, WizardState, WizardStateMachine


@sync_to_async
def save_session(*, session_id: Optional[int], sm: WizardStateMachine,
                 vin: str, ecu_name: str, technician_id: str = "") -> int:
    from ...models import WizardSession  # local import to dodge migration order

    payload = {
        "state": sm.state.value,
        "vin": sm.data.vin or vin,
        "ecu_name": sm.data.ecu_name or ecu_name,
        "captured_isn_hex": sm.data.captured_isn.hex() if sm.data.captured_isn else "",
        "notes": "\n".join(sm.data.notes),
        "error_code": sm.data.error_code,
        "technician_id": technician_id,
    }
    if session_id is None:
        obj = WizardSession.objects.create(**payload)
    else:
        WizardSession.objects.filter(pk=session_id).update(**payload)
        obj = WizardSession.objects.get(pk=session_id)
    return obj.pk


@sync_to_async
def load_session(session_id: int) -> WizardStateMachine:
    from ...models import WizardSession
    obj = WizardSession.objects.get(pk=session_id)
    sm = WizardStateMachine(state=WizardState(obj.state))
    sm.data = WizardData(
        vin=obj.vin, ecu_name=obj.ecu_name,
        captured_isn=bytes.fromhex(obj.captured_isn_hex) if obj.captured_isn_hex else None,
        notes=obj.notes.splitlines() if obj.notes else [],
        error_code=obj.error_code,
    )
    return sm
