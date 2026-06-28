"""N20 Auto-Detect Orchestrator (Phase 2).

The data-driven brain behind one-click "Connect & Read" for N20 (MEVD17.2.9)
and friends:

  Step 1 — Silent Auto-Probe: over ENET, UDS ReadDataByIdentifier pulls the
           live Hardware ID (HWEL / part number) + software version, and reads
           the tamper-protection (TPROT) status.
  Step 2 — Decision Engine:
             • UNLOCKED → standard OBD ISN/coding flow.
             • LOCKED   → look the live Hardware ID up in the Dynamic Hardware
                          Catalog to get the EXACT board-revision bench profile.
  Step 3 — Dynamic Step-by-Step payload: a fully sequential, bilingual guide
           populated entirely from the fetched catalog data (dynamic pins +
           per-board boot image), pausing for technician confirmation before
           the bench extract / code actions.

Nothing about the bench wiring is hardcoded per ECU name — it all flows from
the catalog entry matched to the live Hardware ID. If the ID is unknown we
refuse to invent pins and instead ask the tech to report it.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from ..logging_setup import get_logger
from ..services.chatbot_translator import ChatbotPayload
from ..uds.services import BmwDID, DiagSession
from .ecu_hardware_catalog import (
    HardwareProfile,
    get_hardware_profile_db_first,
)

log = get_logger(__name__)

# TPROT (tamper-protection) status DID. Platform-specific; overridable. A
# single status byte: 0x00 = unlocked, anything else = locked/protected.
TPROT_STATUS_DID = 0xF1B0


class TprotStatus(enum.Enum):
    UNLOCKED = "unlocked"
    LOCKED = "locked"
    UNKNOWN = "unknown"


@dataclass
class HardwareProbe:
    hardware_id: str
    sw_version: str
    tprot: TprotStatus

    @property
    def locked(self) -> bool:
        # Conservative: only an explicit UNLOCKED lets us skip the bench.
        return self.tprot is not TprotStatus.UNLOCKED

    def to_json(self) -> dict[str, Any]:
        return {"hardware_id": self.hardware_id, "sw_version": self.sw_version,
                "tprot": self.tprot.value, "locked": self.locked}


@dataclass
class BenchStep:
    n: int
    kind: str                 # "instruction" | "wiring" | "locate" | "confirm" | "action"
    ar: str
    en: str
    image_url: str = ""
    wires: list[dict] = field(default_factory=list)
    action: str = ""          # for kind=="action": "bench_extract" | "code_ecu"

    def to_json(self) -> dict[str, Any]:
        return {"n": self.n, "kind": self.kind, "ar": self.ar, "en": self.en,
                "image_url": self.image_url, "wires": self.wires,
                "action": self.action}


_COLOR_AR = {
    "red": "الأحمر", "black": "الأسود", "orange": "البرتقالي",
    "brown": "البني", "green": "الأخضر", "yellow": "الأصفر",
}


class N20AutoOrchestrator:
    def __init__(self, *,
                 catalog_lookup: Callable[[str], Optional[HardwareProfile]] = get_hardware_profile_db_first,
                 hw_id_did: int = BmwDID.HW_NUMBER,
                 sw_ver_did: int = BmwDID.SW_VERSION,
                 tprot_did: int = TPROT_STATUS_DID) -> None:
        self.catalog_lookup = catalog_lookup
        self.hw_id_did = hw_id_did
        self.sw_ver_did = sw_ver_did
        self.tprot_did = tprot_did

    # --- Step 1: silent auto-probe ---------------------------------------
    async def probe(self, client) -> HardwareProbe:
        await self._safe_session(client)
        hw = await self._read_str(client, self.hw_id_did)
        sw = await self._read_str(client, self.sw_ver_did)
        tprot = await self._read_tprot(client)
        log.info("auto-probe", extra={"hw": hw, "sw": sw, "tprot": tprot.value})
        return HardwareProbe(hardware_id=hw, sw_version=sw, tprot=tprot)

    async def _safe_session(self, client) -> None:
        try:
            await client.diagnostic_session_control(DiagSession.EXTENDED)
        except Exception:
            pass  # some modules answer identifiers in the default session

    async def _read_str(self, client, did: int) -> str:
        try:
            raw = await client.read_data_by_identifier(did)
        except Exception:
            return ""
        return raw.decode("ascii", "ignore").strip("\x00 ").strip()

    async def _read_tprot(self, client) -> TprotStatus:
        try:
            raw = await client.read_data_by_identifier(self.tprot_did)
        except Exception:
            return TprotStatus.UNKNOWN
        if not raw:
            return TprotStatus.UNKNOWN
        return TprotStatus.UNLOCKED if raw[0] == 0x00 else TprotStatus.LOCKED

    # --- Step 2 + 3: decision engine + dynamic payload -------------------
    async def run(self, client, *, vin: str = "") -> ChatbotPayload:
        probe = await self.probe(client)
        return self.build_payload(probe, vin=vin)

    def build_payload(self, probe: HardwareProbe, *, vin: str = "") -> ChatbotPayload:
        if not probe.locked:
            return self._unlocked_payload(probe, vin)

        profile = self.catalog_lookup(probe.hardware_id)
        if profile is None:
            return self._unknown_hardware_payload(probe)
        return self._bench_payload(probe, profile, vin)

    # --- payload builders -------------------------------------------------
    def _unlocked_payload(self, probe: HardwareProbe, vin: str) -> ChatbotPayload:
        return ChatbotPayload(
            chatbot_message=(
                f"✅ الكنترول مفتوح (TPROT unlocked). Hardware ID: "
                f"{probe.hardware_id or 'غير معروف'}. تقدر تكوّد/تقرأ ISN مباشرة "
                f"على الـ OBD بكابل الـ ENET.\n"
                f"Module is UNLOCKED — proceed with the standard OBD ISN/coding "
                f"flow over ENET."
            ),
            required_action="load_features",
            severity="info",
            diagnostics={
                "flow": "obd_direct",
                "probe": probe.to_json(),
                "vin": vin,
            },
        )

    def _unknown_hardware_payload(self, probe: HardwareProbe) -> ChatbotPayload:
        return ChatbotPayload(
            chatbot_message=(
                f"🔒 الكنترول مقفول، بس الـ Hardware ID "
                f"({probe.hardware_id or 'فاضي'}) مش موجود في كتالوج الهاردوير "
                f"لسه. ابعت الرقم ده لـ Mousstec علشان نضيف مخطط البورده الصح — "
                f"مش هنخمّن البِنّات.\n"
                f"Module is LOCKED but Hardware ID is not in the catalog yet. "
                f"Report it to Mousstec — we won't guess the bench pins."
            ),
            required_action="report_hardware_id",
            severity="warning",
            diagnostics={
                "flow": "unknown_hardware",
                "probe": probe.to_json(),
            },
        )

    def _bench_payload(self, probe: HardwareProbe, profile: HardwareProfile,
                       vin: str) -> ChatbotPayload:
        steps = self._build_steps(profile)
        return ChatbotPayload(
            chatbot_message=(
                f"🔒 الكنترول مقفول (TPROT). اتعرّف عليه: {profile.ecu_name} — "
                f"{profile.board_revision} (HW {profile.hardware_id}). "
                f"اتبع الخطوات بالترتيب لسحب الـ ISN على الطاولة (bench).\n"
                f"LOCKED — detected {profile.ecu_name} {profile.board_revision}. "
                f"Follow the bench steps in order."
            ),
            required_action="confirm_bench_wiring",
            severity="warning",
            visual_aid_url=profile.pinout.pcb_image_url or None,
            diagnostics={
                "flow": "bench",
                "probe": probe.to_json(),
                "vin": vin,
                "hardware": profile.to_json(),
                "steps": [s.to_json() for s in steps],
            },
        )

    def _build_steps(self, profile: HardwareProfile) -> list[BenchStep]:
        p = profile.pinout
        steps: list[BenchStep] = []

        def add(kind: str, ar: str, en: str, *, image_url: str = "",
                wires: Optional[list[dict]] = None, action: str = "") -> None:
            steps.append(BenchStep(n=len(steps) + 1, kind=kind, ar=ar, en=en,
                                   image_url=image_url, wires=wires or [],
                                   action=action))

        add("instruction",
            "افصل كابل الـ ENET من فيشة الـ OBD — مش هنحتاجه في خطوة البنش.",
            "Disconnect the ENET cable from the OBD port — not used on the bench.")
        add("instruction",
            "اطفي الكونتاكت، فك الكنترول من العربية وحطّه على الطاولة. جهّز "
            "واجهة الـ K+DCAN وباور سبلاي 12 فولت.",
            "Ignition off, remove the ECU and place it on the bench. Prepare the "
            "K+DCAN interface and a 12V bench supply.")

        wires = self._wires(profile)
        add("wiring",
            "وصّل البِنّات دي على البورده بالظبط (كل بِن بلونه):",
            "Wire these pins on the board exactly (each pin by colour):",
            image_url=p.pcb_image_url, wires=wires)

        if p.boot_pin is not None:
            add("locate",
                f"حدّد بِن البوت رقم {p.boot_pin} من الصورة دي — ده مكانه على "
                f"بوردتك بالظبط ({profile.board_revision}). نزّله على GND لحظة "
                f"التشغيل لثانية واحدة.",
                f"Locate BOOT pin {p.boot_pin} in this photo — exact spot for "
                f"your board ({profile.board_revision}). Ground it for one "
                f"second on power-up.",
                image_url=p.boot_image_url)

        for ar, en in zip(profile.physical_steps_ar, profile.physical_steps_en):
            add("instruction", ar, en)

        add("confirm",
            "أكّد إنك وصّلت كل البِنّات صح وإن بِن البوت في مكانه قبل ما نكمّل.",
            "Confirm all pins are wired correctly and the boot pin is located "
            "before continuing.")
        add("action",
            "نفّذ سحب الـ ISN من البنش.",
            "Execute the bench ISN extract.",
            action="bench_extract")
        add("action",
            "بعد سحب الـ ISN بنجاح: كوّد/برمج الكنترول.",
            "After a successful ISN extract: code/program the ECU.",
            action="code_ecu")
        return steps

    def _wires(self, profile: HardwareProfile) -> list[dict]:
        p = profile.pinout
        rows = [
            ("power", p.power_pin, "تغذية +12 فولت (KL30)", "+12V (KL30)", "red"),
            ("ground", p.ground_pin, "أرضي (KL31)", "Ground (KL31)", "black"),
        ]
        if p.can_h_pin is not None and p.can_l_pin is not None:
            rows.append(("can_h", p.can_h_pin, "CAN-High", "CAN-High", "orange"))
            rows.append(("can_l", p.can_l_pin, "CAN-Low", "CAN-Low", "brown"))
        elif p.k_line_pin is not None:
            rows.append(("k_line", p.k_line_pin, "K-Line", "K-Line", "green"))

        out: list[dict] = []
        for fn, pin, lab_ar, lab_en, color in rows:
            if pin is None:
                continue
            out.append({
                "function": fn, "ecu_pin": pin, "color": color,
                "label_ar": f"{lab_ar} — السلك {_COLOR_AR.get(color, color)}",
                "label_en": f"{lab_en} ({color})",
            })
        return out
