"""CodingOrchestrator — sister of ApiOrchestrator, scoped to the Coding flow.

Does NOT replace the existing orchestrator. The existing /execute view
branches on `operation_type`: "isn" → ApiOrchestrator (unchanged),
"coding" → CodingOrchestrator (this module).

Two coding actions surface to the chatbot:

    action="list_features"      → returns a menu of FdlFeatures for the
                                  given chassis as a chatbot input_schema.
    action="apply_feature"      → applies a chosen FdlFeature
                                  (enable/disable).
    action="initialize_module"  → applies the live FA to a replaced
                                  module (EPS/EGS/DME/FEM).

All three return the same standard ChatbotPayload-based JSON shape the
ISN flow already uses — no schema change.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from ..coding import (
    apply_feature, feature_to_chatbot_option, get_feature,
    initialize_replaced_module, list_features, read_vo_from_vcm,
)
from ..coding.fa_vo import VehicleOrder, parse_fa
from ..coding.fdl_features import FdlCategory
from ..execution.base import StrategyContext
from ..logging_setup import get_logger
from ..services.billing_gate import AbstractBillingGate, CodingEntitlement
from ..services.chatbot_translator import ChatbotPayload, translate_exception
from ..uds.services import DiagSession

log = get_logger(__name__)


@dataclass
class CodingResponse:
    chatbot: ChatbotPayload
    outcome: str
    entitlement: dict[str, Any]
    next_endpoint: str = ""

    def to_json(self) -> dict[str, Any]:
        body = self.chatbot.to_json()
        body.update(
            outcome=self.outcome,
            entitlement=self.entitlement,
            next_endpoint=self.next_endpoint,
            # Mirror the ISN response's billing key for chatbot UI uniformity.
            billing={"operation_type": self.entitlement.get("operation_type"),
                     "mode": self.entitlement.get("mode")},
        )
        return body


class CodingOrchestrator:
    """Sits next to ApiOrchestrator. The existing chatbot doesn't change."""

    def __init__(self, billing: AbstractBillingGate) -> None:
        self.billing = billing

    async def run(self, *, ctx: StrategyContext,
                  coding_request: dict[str, Any]) -> CodingResponse:
        action = coding_request.get("action", "list_features")

        # 1. Entitlement gate — same call regardless of action.
        try:
            entitlement: CodingEntitlement = (
                await self.billing.verify_coding_subscription_or_hold(
                    vin=ctx.vin, operation_type="coding",
                )
            )
        except Exception as e:
            return CodingResponse(
                chatbot=translate_exception(e),
                outcome="entitlement_error", entitlement={},
            )

        if not entitlement.entitled and entitlement.mode == "denied":
            return CodingResponse(
                chatbot=ChatbotPayload(
                    chatbot_message=(
                        "🔒 الـ Coding add-on مش مفعّل لورشتك. تواصل مع "
                        "Mousstec لتفعيل الاشتراك.\n"
                        "Coding add-on not active — contact Mousstec to subscribe."
                    ),
                    required_action="contact_billing", severity="error",
                ),
                outcome="entitlement_denied",
                entitlement=self._entitlement_dict(entitlement),
            )

        if not entitlement.entitled and entitlement.mode == "hold":
            # Tenant doesn't have a subscription but a hold was placed.
            # Product policy = let them proceed; pricing TBD. Chatbot is
            # notified so the workshop knows it's not free indefinitely.
            log.info("Coding proceeds under hold", extra={
                "vin": ctx.vin, "hold": entitlement.hold_ref,
            })

        # 2. Dispatch on action.
        try:
            if action == "list_features":
                return await self._list_features(ctx, coding_request, entitlement)
            if action == "apply_feature":
                return await self._apply_feature(ctx, coding_request, entitlement)
            if action == "initialize_module":
                return await self._initialize_module(ctx, coding_request, entitlement)
            return CodingResponse(
                chatbot=ChatbotPayload(
                    chatbot_message=f"Unknown coding action: {action!r}",
                    required_action="restart_session", severity="error",
                ),
                outcome="bad_request",
                entitlement=self._entitlement_dict(entitlement),
            )
        except Exception as e:
            return CodingResponse(
                chatbot=translate_exception(e),
                outcome="error",
                entitlement=self._entitlement_dict(entitlement),
            )

    # --- Action: list_features -------------------------------------------
    async def _list_features(self, ctx: StrategyContext,
                             req: dict[str, Any],
                             entitlement: CodingEntitlement) -> CodingResponse:
        chassis = req.get("chassis") or (
            ctx.profile.chassis[0] if ctx.profile.chassis else None
        )
        category = req.get("category")
        cat_enum = FdlCategory(category) if category else None
        features = list_features(chassis=chassis, category=cat_enum)
        options = [feature_to_chatbot_option(f) for f in features]

        return CodingResponse(
            chatbot=ChatbotPayload(
                chatbot_message=(
                    f"📋 لقيت {len(options)} ميزة مخفية متاحة للـ "
                    f"{chassis or 'الشاسيه ده'}. اختار اللي تريد تفعيله "
                    f"أو إيقافه.\n"
                    f"Found {len(options)} hidden features for {chassis or 'this chassis'}."
                ),
                required_action="choose_feature",
                severity="info",
                input_schema={
                    "type": "object",
                    "properties": {
                        "feature_id": {"type": "string",
                                       "enum": [o["id"] for o in options]},
                        "enable": {"type": "boolean"},
                    },
                    "required": ["feature_id", "enable"],
                    "x_options": options,        # chatbot UI renders this list
                },
            ),
            outcome="awaiting_choice",
            entitlement=self._entitlement_dict(entitlement),
            next_endpoint="/api/ecu/execute",
        )

    # --- Action: apply_feature -------------------------------------------
    async def _apply_feature(self, ctx: StrategyContext,
                             req: dict[str, Any],
                             entitlement: CodingEntitlement) -> CodingResponse:
        feature_id = req["feature_id"]
        enable = bool(req.get("enable", True))
        feature = get_feature(feature_id)

        from ..uds.client import UdsClient
        client = UdsClient(ctx.transport,
                           ecu_addr=feature.did >> 8,
                           session_name="coding_apply")

        await apply_feature(client, ctx.security, feature=feature,
                            enable=enable, vin=ctx.vin)

        verb_ar = "فعّلت" if enable else "أوقفت"
        verb_en = "Enabled" if enable else "Disabled"
        return CodingResponse(
            chatbot=ChatbotPayload(
                chatbot_message=(
                    f"✅ {verb_ar} «{feature.name_ar}». لازم إعادة تشغيل "
                    f"الـ ignition لتفعيل التغيير.\n"
                    f"{verb_en} \"{feature.name_en}\". Cycle ignition to activate."
                ),
                required_action="cycle_ignition",
                severity="info",
                diagnostics={"feature": feature.id,
                             "ecu": feature.ecu_target,
                             "did": f"0x{feature.did:04X}"},
            ),
            outcome="success",
            entitlement=self._entitlement_dict(entitlement),
        )

    # --- Action: initialize_module ---------------------------------------
    async def _initialize_module(self, ctx: StrategyContext,
                                 req: dict[str, Any],
                                 entitlement: CodingEntitlement) -> CodingResponse:
        module_id = req["module_id"]

        from ..uds.client import UdsClient
        client = UdsClient(ctx.transport, ecu_addr=ctx.profile.uds_isn_did >> 8,
                           session_name="coding_init")

        # Read live FA from the VCM (or accept provided VO ASCII as override).
        if "fa_ascii_override" in req:
            vo: VehicleOrder = parse_fa(req["fa_ascii_override"])
        else:
            vo = await read_vo_from_vcm(client)

        async def dump() -> bytes:
            try:
                return await client.read_data_by_identifier(0xF015)
            except Exception:
                return b"\x00" * 8

        result = await initialize_replaced_module(
            client=client, security=ctx.security, preflight=ctx.preflight,
            vin=ctx.vin, module_id=module_id, vo=vo, backup_dump=dump,
        )

        verified_ar = "والتحقق نجح" if result.verified else "بس التحقق لسة محتاج مراجعة"
        verified_en = "verified" if result.verified else "verify pending"
        return CodingResponse(
            chatbot=ChatbotPayload(
                chatbot_message=(
                    f"🛠 الـ {module_id} اتعمله initialize بـ "
                    f"{result.coded_options_count} option من الـ FA "
                    f"{verified_ar}.\n"
                    f"{module_id} initialised with {result.coded_options_count} "
                    f"options from FA — {verified_en}."
                ),
                required_action="cycle_ignition" if result.verified
                                else "manual_verify",
                severity="info" if result.verified else "warning",
                diagnostics={"module_id": module_id,
                             "coded_options_count": result.coded_options_count,
                             "verified": result.verified,
                             "notes": result.notes},
            ),
            outcome="success" if result.verified else "partial",
            entitlement=self._entitlement_dict(entitlement),
        )

    # --- helpers ----------------------------------------------------------
    @staticmethod
    def _entitlement_dict(e: CodingEntitlement) -> dict[str, Any]:
        return {
            "entitled": e.entitled,
            "operation_type": e.operation_type,
            "mode": e.mode,
            "subscription_ref": e.subscription_ref,
            "hold_ref": e.hold_ref,
            "reason": e.reason,
        }
