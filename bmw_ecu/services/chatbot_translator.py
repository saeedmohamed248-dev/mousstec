"""AI Troubleshooter — turns exceptions + StrategyResults into chatbot JSON.

Keep this module **pure** (no DB / no I/O). Easy to unit-test, easy to
plug a different localisation table later.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from ..exceptions import (
    BmwEcuError,
    BoxNotConnectedError,
    CANLinesReversedError,
    ECUNoPowerError,
    FeeAuthorizationDeclined,
    FlashRollbackFailed,
    IgnitionOffError,
    IsnMismatch,
    LowVoltage,
    NoInterfaceDetected,
    SecurityAccessDenied,
    TransportTimeout,
    UdsNegativeResponse,
)
from ..execution.base import StrategyOutcome, StrategyResult


@dataclass
class ChatbotPayload:
    """Conversational JSON contract returned to the AI Chatbot UI."""

    chatbot_message: str                              # bilingual: Arabic + English
    required_action: str = ""                         # next user move
    severity: str = "info"                            # info|warning|error|critical
    visual_aid_url: Optional[str] = None
    input_schema: Optional[dict[str, Any]] = None
    suggestions: list[str] = field(default_factory=list)
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def merge(self, **kwargs: Any) -> "ChatbotPayload":
        for k, v in kwargs.items():
            if v is not None:
                setattr(self, k, v)
        return self

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Exception → chatbot
# ---------------------------------------------------------------------------
def translate_exception(exc: BaseException) -> ChatbotPayload:
    """Map a known exception to a user-facing payload.

    Unknown exceptions → generic "internal error" with the error class name
    in diagnostics so the support team can pull the traceback from logs.
    """
    if isinstance(exc, ECUNoPowerError):
        return ChatbotPayload(
            chatbot_message=(
                "⚡ مفيش كهربا للـ ECU. اتأكد إن KL30 (12V) موصّل وإن KL31 (GND) "
                "مش مفصول.\nNo power detected — check 12V supply and ground."
            ),
            required_action="verify_power",
            severity="error",
            suggestions=[
                "اتأكد من الـ fuse box على لوحة الكهرباء.",
                "قِس الفولت على pin V12 — لازم يقرأ ≥12V.",
                "لو على bench: راجع وصلات الـ Smart Box.",
            ],
        )
    if isinstance(exc, CANLinesReversedError):
        return ChatbotPayload(
            chatbot_message=(
                "🔌 يبدو إن CAN High و CAN Low معكوسين. الـ ECU بيرد بـ frames "
                "مقلوبة.\nCAN High/Low look reversed — swap the two wires."
            ),
            required_action="swap_can_lines",
            severity="error",
            suggestions=["اعكس CAN_H ↔ CAN_L وحاول تاني."],
        )
    if isinstance(exc, IgnitionOffError):
        return ChatbotPayload(
            chatbot_message=(
                "🔑 الـ ignition مقفول. لف المفتاح على وضع KL15 (ON بدون تشغيل) "
                "وكمل.\nIgnition off — turn key to ON (KL15) and retry."
            ),
            required_action="verify_ignition",
            severity="warning",
        )
    if isinstance(exc, BoxNotConnectedError):
        return ChatbotPayload(
            chatbot_message=(
                "📦 الـ Mousstec Smart Box مش رادد على الـ USB. "
                "افصل وارجع وصّل الكابل.\nSmart Box not responding on USB."
            ),
            required_action="reconnect_smart_box",
            severity="error",
        )
    if isinstance(exc, LowVoltage):
        v = exc.context.get("volts", "?")
        return ChatbotPayload(
            chatbot_message=(
                f"🔋 البطارية ضعيفة ({v}V). وصّل شاحن قبل ما نكمل عشان "
                f"الـ flash مايقطعش في النص.\n"
                f"Battery low ({v}V) — connect a charger before flashing."
            ),
            required_action="connect_charger",
            severity="error",
            suggestions=["وصّل شاحن CTEK أو أي charger يدعم 13.5V+.",
                         "لما الفولت يطلع فوق 13V، رجع جرّب."],
        )
    if isinstance(exc, NoInterfaceDetected):
        return ChatbotPayload(
            chatbot_message=(
                "🔍 مش لاقي ENET ولا K+DCAN ولا SocketCAN. اتأكد من الكابل "
                "والاتصال.\nNo OBD interface detected."
            ),
            required_action="check_obd_cable",
            severity="error",
            suggestions=["جرّب كابل ENET تاني.",
                         "اتأكد إن الـ IP على الـ laptop بتدي 169.254.x.x."],
        )
    if isinstance(exc, TransportTimeout):
        return ChatbotPayload(
            chatbot_message=(
                "⏱ الـ ECU مارَدّش في الوقت المحدد. غالباً ignition مقفول أو "
                "الـ gateway مش فاتح القناة.\nECU did not respond in time."
            ),
            required_action="verify_ignition",
            severity="warning",
        )
    if isinstance(exc, SecurityAccessDenied):
        return ChatbotPayload(
            chatbot_message=(
                "🔒 الأمان رفض الـ key. غالباً الـ ECU محتاج boot mode أو "
                "بـ firmware version مش موجودة فيها exploit.\n"
                "Security access denied — may require BDM or wizard path."
            ),
            required_action="escalate_strategy",
            severity="warning",
            suggestions=["الـ Manager هيجرّب hardware automation أو wizard.",
                         "لو ECU MEVD17 → محتاج BDM probe."],
        )
    if isinstance(exc, UdsNegativeResponse):
        return ChatbotPayload(
            chatbot_message=(
                f"❌ الـ ECU رفض الطلب (SID=0x{exc.sid:02X}, NRC=0x{exc.nrc:02X}). "
                f"عادةً يعني conditions not correct أو session غلط.\n"
                f"UDS negative response — see diagnostics."
            ),
            required_action="retry_or_escalate",
            severity="warning",
            diagnostics={"sid": exc.sid, "nrc": exc.nrc},
        )
    if isinstance(exc, IsnMismatch):
        return ChatbotPayload(
            chatbot_message=(
                "🆔 الـ ISN اللي اتقرأ مش متطابق. ممكن يكون 0x00/0xFF (virgin) "
                "أو الـ read-back مش بيطابق اللي كتبناه.\n"
                "ISN mismatch — check source ECU."
            ),
            required_action="abort_and_inspect",
            severity="error",
        )
    if isinstance(exc, FlashRollbackFailed):
        return ChatbotPayload(
            chatbot_message=(
                "🚨 توقف فوراً! الـ flash فشل والـ rollback فشل كمان. "
                "متطفّيش العربية، متقلعش الـ ECU، اتصل بالـ senior تكنيشين "
                "حالاً.\n"
                "CRITICAL — flash + rollback failed. DO NOT POWER CYCLE. "
                "Call senior technician immediately."
            ),
            required_action="halt_and_call_senior",
            severity="critical",
        )
    if isinstance(exc, FeeAuthorizationDeclined):
        return ChatbotPayload(
            chatbot_message=(
                "💳 الـ payment authorization اترفض. الورشة محتاجة تجدّد "
                "الاشتراك أو ترفع رصيد.\n"
                "Fee authorization declined — workshop balance issue."
            ),
            required_action="contact_billing",
            severity="error",
        )
    if isinstance(exc, BmwEcuError):
        return ChatbotPayload(
            chatbot_message=(
                f"حصلت مشكلة غير متوقعة: {exc.code}. الـ session اتقفل بأمان."
                f"\nUnexpected subsystem error: {exc.code}."
            ),
            required_action="restart_session",
            severity="error",
            diagnostics={"code": exc.code, "message": str(exc)},
        )
    return ChatbotPayload(
        chatbot_message=(
            "حصل خطأ داخلي. الـ session اتلغي ومفيش رسوم انحسبت.\n"
            "Internal error — session aborted, no fee charged."
        ),
        required_action="restart_session",
        severity="critical",
        diagnostics={"class": exc.__class__.__name__, "repr": repr(exc)[:200]},
    )


# ---------------------------------------------------------------------------
# StrategyResult → chatbot
# ---------------------------------------------------------------------------
def translate_result(result: StrategyResult) -> ChatbotPayload:
    """Map a terminal or suspended StrategyResult to a chatbot message."""
    if result.outcome == StrategyOutcome.SUCCESS:
        return ChatbotPayload(
            chatbot_message=(
                "✅ تمام! الـ ISN اتعمله sync بنجاح والـ ECU اتقفل."
                "\nSuccess — ISN synchronised and ECU finalised."
            ),
            required_action="session_complete",
            severity="info",
            diagnostics={"strategy": result.strategy_name,
                         "backup_sha256": result.backup_sha256[:12]},
        )
    if result.outcome == StrategyOutcome.SUSPENDED:
        step = (result.wizard_next_step or {}).get("step", {})
        return ChatbotPayload(
            chatbot_message=step.get("instructions", "كمّل الخطوة التالية."),
            required_action=step.get("kind", "wizard_step"),
            severity="info",
            visual_aid_url=step.get("pinout_diagram_url"),
            input_schema=step.get("input_schema"),
            diagnostics={"wizard_session_id":
                         (result.wizard_next_step or {}).get("session_id"),
                         "title": step.get("title", "")},
        )
    if result.outcome in (StrategyOutcome.FAILED_ROLLED_BACK,
                         StrategyOutcome.PARTIAL):
        return ChatbotPayload(
            chatbot_message=(
                f"⚠️ الـ {result.strategy_name} فشل واتعمل rollback آمن. "
                f"السبب: {result.error_code or result.error_message or 'غير معروف'}."
                f"\nStrategy failed and rolled back — no charge applied."
            ),
            required_action="retry_with_fallback",
            severity="warning",
            diagnostics={"strategy": result.strategy_name,
                         "code": result.error_code,
                         "message": result.error_message},
        )
    if result.outcome == StrategyOutcome.FAILED_UNRECOVERABLE:
        return ChatbotPayload(
            chatbot_message=(
                "🚨 فشل غير قابل للاسترداد. الـ ECU في حالة غير معروفة. "
                "اتصل بالـ senior تكنيشين فوراً.\n"
                "UNRECOVERABLE — call senior technician now."
            ),
            required_action="halt_and_call_senior",
            severity="critical",
            diagnostics={"code": result.error_code,
                         "message": result.error_message},
        )
    return ChatbotPayload(
        chatbot_message="حالة غير معروفة من الـ Manager.",
        required_action="restart_session",
        severity="warning",
        diagnostics={"outcome": result.outcome.value},
    )
