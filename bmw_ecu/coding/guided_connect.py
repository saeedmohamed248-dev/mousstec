"""Real-world guided connect → read → code assessment.

This is the brain behind the Coding Room's "اتصال وقراءة / Connect & Read"
button. A technician with a real ENET (or K+D-CAN) cable clicks Connect;
the backend reads the car and then tells them — in plain bilingual steps —
exactly what to do next.

  • OPEN module (low protection + known software path):
    "كوّد مباشرة بكابل الاي نت" → straight to Load features / Apply.

  • LOCKED module (HIGH/CRITICAL, or bench-only): we hand back a FULL
    wire-by-wire bench procedure. The key idea: the technician already
    holds a standard OBD-II (J1962) plug on their D-CAN cable, so we tell
    them which OBD-II pin connects to which ECU bench pin — e.g.

        OBD pin 16 (+12V)  →  ECU pin 87 (KL30)
        OBD pin 4  (GND)   →  ECU pin 88 (GND)
        OBD pin 7  (K-Line)→  ECU pin 63 (K-Line)
        then momentarily ground BOOT pin 24 on power-up.

    Both ends are real: the OBD side is the fixed J1962 standard, the ECU
    side is pulled live from the pinout repo, so the map always matches the
    actual connector for that ECU family. A junior tech can follow it
    socket-to-socket without prior knowledge.

Pure-async, hardware-free. Fully unit-tested.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from ..execution.ecu_profiles import EcuProfile, ProtectionLevel
from ..execution.interactive_guided.pinout_repository import (
    PinoutDiagram, PinoutRepository,
)
from ..uds.services import DiagSession


# --- Standard OBD-II (SAE J1962) connector — the plug on the tech's cable ----
# function-key → (obd_pin, label_ar, label_en, wire_color)
OBD2_PINOUT: dict[str, tuple[int, str, str, str]] = {
    "12v":   (16, "تغذية +12 فولت (KL30)", "+12V Battery (KL30)", "red"),
    "gnd":   (4,  "أرضي الشاسيه (GND)", "Chassis Ground", "black"),
    "sgnd":  (5,  "أرضي الإشارة", "Signal Ground", "black"),
    "canh":  (6,  "CAN-High (HS-CAN)", "CAN-High (HS-CAN)", "orange"),
    "canl":  (14, "CAN-Low (HS-CAN)", "CAN-Low (HS-CAN)", "brown"),
    "kline": (7,  "K-Line (ISO 9141 / KWP2000)", "K-Line (ISO 9141 / KWP2000)", "green"),
}


@dataclass
class WireConnection:
    """One physical wire: from an OBD-II pin to an ECU bench pin."""
    function: str                 # "12v" | "gnd" | "canh" | "canl" | "kline"
    obd_pin: int
    ecu_pin: int
    label_ar: str
    label_en: str
    color: str

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class GuidedStep:
    n: int
    ar: str
    en: str

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class LiveIdentifier:
    """One real value pulled off the ECU during Connect & Read."""
    did: int
    name_ar: str
    name_en: str
    value: str
    raw_hex: str

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        d["did"] = f"0x{self.did:04X}"
        return d


@dataclass
class LiveRead:
    """Result of actually talking to the ECU (not a canned message).

    `reachable` is True when the ECU answered *anything* — either an extended
    diagnostic session was accepted, or at least one identifier came back.
    When it's False, the module simply isn't on the wire (cable off, ignition
    off, wrong pins on the bench) and we must NOT pretend to know open/locked.
    """
    reachable: bool
    identifiers: list[LiveIdentifier] = field(default_factory=list)
    session_ok: bool = False
    notes: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "reachable": self.reachable,
            "session_ok": self.session_ok,
            "identifiers": [i.to_json() for i in self.identifiers],
            "notes": list(self.notes),
        }


# DIDs we try to pull on every real connect, in display order.
# (did, name_ar, name_en, decode_kind)
_LIVE_DIDS: list[tuple[int, str, str, str]] = [
    (0xF190, "رقم الشاسيه (VIN)", "VIN", "ascii"),
    (0xF195, "إصدار السوفت وير", "Software version", "ascii"),
    (0xF191, "رقم الهاردوير", "Hardware number", "ascii"),
    (0xF18C, "السيريال", "ECU serial", "ascii"),
    (0xF199, "تاريخ البرمجة", "Programming date", "hex"),
    (0xF40C, "فولت البطارية", "Battery voltage", "volts"),
]


def _decode_value(raw: bytes, kind: str) -> str:
    if not raw:
        return ""
    if kind == "volts":
        if len(raw) >= 2:
            return f"{int.from_bytes(raw[:2], 'big') / 100:.1f} V"
        return raw.hex()
    if kind == "ascii":
        s = raw.decode("ascii", "ignore").strip("\x00 ").strip()
        return s or raw.hex()
    return raw.hex()


async def read_live(client, *,
                    did_list: Optional[list[tuple[int, str, str, str]]] = None
                    ) -> LiveRead:
    """Actually read the ECU over UDS. Hardware errors degrade gracefully.

    `client` is a UdsClient already bound to the right ECU address/transport.
    We open an EXTENDED session (read-only, no security) and read each known
    identifier, collecting whatever responds. Nothing here writes or unlocks.
    """
    dids = did_list or _LIVE_DIDS
    idents: list[LiveIdentifier] = []
    notes: list[str] = []

    session_ok = False
    try:
        await client.diagnostic_session_control(DiagSession.EXTENDED)
        session_ok = True
    except Exception as e:  # timeout / NRC / transport down
        notes.append(f"extended_session: {e}")

    for did, name_ar, name_en, kind in dids:
        try:
            raw = await client.read_data_by_identifier(did)
        except Exception:
            continue  # this DID not supported by this ECU — skip silently
        if not raw:
            continue
        idents.append(LiveIdentifier(
            did=did, name_ar=name_ar, name_en=name_en,
            value=_decode_value(raw, kind), raw_hex=raw.hex(),
        ))

    reachable = session_ok or bool(idents)
    return LiveRead(reachable=reachable, identifiers=idents,
                    session_ok=session_ok, notes=notes)


@dataclass
class ConnectAssessment:
    """The verdict returned to the frontend after Connect & Read."""
    vin: str
    ecu_name: str
    chassis: str
    engine: str
    protection: str                       # "OPEN".."CRITICAL"
    locked: bool
    cable: str                            # "enet" | "dcan_bench"
    headline_ar: str
    headline_en: str
    reachable: bool = True
    pinout_diagram_url: Optional[str] = None
    pinout_callouts: list[dict] = field(default_factory=list)
    wiring: list[WireConnection] = field(default_factory=list)
    boot_pin: Optional[int] = None
    steps: list[GuidedStep] = field(default_factory=list)
    identifiers: list[dict] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        d["steps"] = [s.to_json() for s in self.steps]
        d["wiring"] = [w.to_json() for w in self.wiring]
        return d


# --- pin extraction ---------------------------------------------------------
def _find_pin(callouts: list[dict], *keywords: str) -> Optional[int]:
    """First callout whose label contains ANY keyword (case-insensitive)."""
    for c in callouts:
        label = str(c.get("label", "")).lower()
        if any(k.lower() in label for k in keywords):
            pin = c.get("pin")
            if isinstance(pin, int):
                return pin
            m = re.search(r"\d+", str(pin or ""))
            if m:
                return int(m.group())
    return None


# --- wiring map -------------------------------------------------------------
def _build_wiring(callouts: list[dict]) -> list[WireConnection]:
    """Map standard OBD-II pins to the live ECU bench pins.

    We only add a wire when we actually know the ECU-side pin (from the
    pinout), so the tech never sees a half-specified connection. Comms
    adapts automatically: a CAN connector gets CAN-H/CAN-L wires, a K-Line
    connector gets the K-Line wire.
    """
    ecu = {
        "12v":   _find_pin(callouts, "kl30", "12v"),
        "gnd":   _find_pin(callouts, "kl31", "gnd", "ground"),
        "canh":  _find_pin(callouts, "can high", "can-h", "can h"),
        "canl":  _find_pin(callouts, "can low", "can-l", "can l"),
        "kline": _find_pin(callouts, "k-line", "kline", "k line"),
    }

    order = ["12v", "gnd"]
    if ecu["canh"] is not None and ecu["canl"] is not None:
        order += ["canh", "canl"]
    elif ecu["kline"] is not None:
        order += ["kline"]

    wiring: list[WireConnection] = []
    for fn in order:
        ecu_pin = ecu.get(fn)
        if ecu_pin is None or fn not in OBD2_PINOUT:
            continue
        obd_pin, lab_ar, lab_en, color = OBD2_PINOUT[fn]
        wiring.append(WireConnection(
            function=fn, obd_pin=obd_pin, ecu_pin=ecu_pin,
            label_ar=lab_ar, label_en=lab_en, color=color,
        ))
    return wiring


_COLOR_AR = {
    "red": "الأحمر", "black": "الأسود", "orange": "البرتقالي",
    "brown": "البني", "green": "الأخضر", "yellow": "الأصفر",
}


# --- step builders ----------------------------------------------------------
def _not_connected_steps() -> list[GuidedStep]:
    """The ECU answered nothing — tell the tech how to get it on the wire."""
    return [
        GuidedStep(
            1,
            "الكنترول مردّش ❌ — يعني لسه مش واصل. اتأكد إن الكونتاكت ON "
            "(المفتاح على وضع التشغيل من غير ما تدوّر الموتور).",
            "The ECU didn't answer ❌ — it's not on the bus yet. Make sure "
            "ignition is ON (key in run position, engine not cranked).",
        ),
        GuidedStep(
            2,
            "اتأكد إن كابل الاي نت / الـ OBD داخل صح في فيشة الأعطال "
            "(J1962) لحد ما يثبت، وإن البطارية فولتها كويس (أعلى من 12 فولت).",
            "Check the ENET / OBD cable is fully seated in the OBD-II "
            "(J1962) port and the battery is healthy (above 12 V).",
        ),
        GuidedStep(
            3,
            "لو شغّال على bench: راجع توصيل الباور (12V) والأرضي وأطراف "
            "الاتصال على الكنترول، وبعدين اضغط «🔌 Connect & Read» تاني.",
            "If on the bench: re-check the 12V, ground and comms pins on the "
            "module, then press “🔌 Connect & Read” again.",
        ),
    ]


def _open_steps() -> list[GuidedStep]:
    return [
        GuidedStep(
            1,
            "الكنترول مفتوح ✅ — سيب كابل الاي نت (ENET) متوصّل في فيشة OBD "
            "والكونتاكت ON.",
            "Module is OPEN ✅ — keep the ENET cable in the OBD port with "
            "ignition ON.",
        ),
        GuidedStep(
            2,
            "اضغط «📋 Load features» علشان السيستم يقرا الميزات المتاحة.",
            "Press “📋 Load features” to read the available features.",
        ),
        GuidedStep(
            3,
            "اختار الميزة اللي عايز تفعّلها أو توقفها واضغط «🚀 Apply». "
            "بعد كده دوّر المفتاح OFF/ON.",
            "Pick the feature(s) to enable/disable and press “🚀 Apply”, "
            "then cycle the ignition.",
        ),
    ]


def _locked_steps(profile: EcuProfile,
                  wiring: list[WireConnection],
                  boot: Optional[int],
                  uses_can: bool) -> list[GuidedStep]:
    """Detailed, junior-friendly bench procedure with explicit wiring."""
    ecu = profile.name
    steps: list[GuidedStep] = []

    def add(ar: str, en: str) -> None:
        steps.append(GuidedStep(len(steps) + 1, ar, en))

    add(
        "🔒 الكنترول مقفول (حماية عالية) — مش هينفع نكتب عليه وهو راكب في "
        "العربية. هنفكّه ونشتغل عليه على الطاولة (bench).",
        f"🔒 The {ecu} is LOCKED (high protection) — it can't be written "
        "in-car. We'll remove it and work on the bench.",
    )
    add(
        "أوّل حاجة: اطفي الكونتاكت (Ignition OFF) وافصل طرف البطارية الموجب "
        "واستنى 5 دقايق قبل ما تفك أي حاجة.",
        "First: switch ignition OFF, disconnect the battery positive, and "
        "wait 5 minutes before removing anything.",
    )
    add(
        f"فك الكنترول {ecu} من مكانه وافصل كل الفيش عنه، وحطّه قدامك على "
        "طاولة الشغل.",
        f"Remove the {ecu} module, unplug all its connectors, and place it "
        "in front of you on the bench.",
    )
    add(
        "وصّل واجهة الـ D-CAN في اللاب توب (USB) وافتح البرنامج — سيبها جنبك "
        "وفيشة الـ OBD (الذكر) في إيدك، دي اللي هنوصّل منها للكنترول سلك سلك.",
        "Plug the D-CAN interface into the laptop (USB) and open the "
        "software. Keep the OBD-II (male) plug in hand — we'll wire it to "
        "the ECU pin by pin.",
    )

    # The wire-by-wire heart of it.
    for w in wiring:
        color_ar = _COLOR_AR.get(w.color, w.color)
        add(
            f"وصّل سلكة من بِن {w.obd_pin} ({w.label_ar}) في فيشة الـ OBD "
            f"← لِبِن {w.ecu_pin} في كنترول الموتور. (السلك {color_ar})",
            f"Run a wire from OBD pin {w.obd_pin} ({w.label_en}) → to ECU "
            f"pin {w.ecu_pin}. (wire colour: {w.color})",
        )

    if uses_can:
        add(
            "اتأكد إن أطراف الـ CAN مش متعكوسة (H مع H و L مع L) — لو عكستهم "
            "البرنامج مش هيلاقي الكنترول.",
            "Double-check the CAN pair isn't reversed (H↔H, L↔L) — if "
            "swapped, the software won't see the module.",
        )

    if boot is not None:
        add(
            f"دلوقتي ادّي الباور (12V) وفي نفس اللحظة نزّل بِن البوت رقم "
            f"{boot} (الأصفر في المخطط) على GND لثانية واحدة بس — كده "
            "الكنترول دخل وضع الـ bootloader (BSL) وبقى جاهز للقراءة/الكتابة.",
            f"Now apply power (12V) and at the same instant momentarily "
            f"ground BOOT pin {boot} (yellow) for one second — the module "
            "enters bootloader (BSL) mode, ready to read/write.",
        )
    else:
        add(
            "ادّي الباور (12V) للكنترول — بقى جاهز.",
            "Apply power (12V) to the module — it's ready.",
        )

    add(
        "في البرنامج اضغط «اقرا/Read» — لازم يلاقي الكنترول ويطلّع الـ VIN "
        "والإصدار. لو مالقاش حاجة: راجع الأرضي والباور والأطراف تاني.",
        "In the software press “Read” — it should detect the module and "
        "show the VIN + version. If nothing: re-check ground, power, and "
        "the pins.",
    )
    add(
        "👆 المخطط الملوّن فوق هو كنترولك بالظبط — طابق كل سلكة على رقم "
        "ولون البِن قبل ما تدوس Read، وما تكتبش أي حاجة قبل ما القراءة تنجح.",
        "👆 The coloured diagram above is your exact module — match every "
        "wire to the pin number/colour before pressing Read, and don't "
        "write anything until the read succeeds.",
    )
    return steps


# --- public API -------------------------------------------------------------
async def assess_connection(
    *,
    profile: EcuProfile,
    vin: str,
    chassis: str = "",
    pinout_repo: Optional[PinoutRepository] = None,
    live: Optional[LiveRead] = None,
) -> ConnectAssessment:
    """Decide OPEN vs LOCKED and build the matching guided procedure.

    `live` is the result of an actual UDS read. When supplied and the ECU was
    NOT reachable, we return a NOT_CONNECTED verdict (check cable/ignition)
    instead of pretending to know whether it's open or locked. When the ECU
    IS reachable, the real identifiers it returned are attached so the UI can
    show the technician the module's actual number and data. When `live` is
    None we keep the original static (capability-only) behaviour so the unit
    tests and any caller without hardware still work unchanged.
    """
    chassis = chassis or (profile.chassis[0] if profile.chassis else "")
    identifiers = [i.to_json() for i in live.identifiers] if live else []

    # Real read, but nothing answered → don't fake an open/locked verdict.
    if live is not None and not live.reachable:
        return ConnectAssessment(
            vin=vin,
            ecu_name=profile.name,
            chassis=chassis,
            engine=profile.engine,
            protection=ProtectionLevel(profile.protection).name,
            locked=False,
            reachable=False,
            cable="enet",
            headline_ar=(
                f"معرفتش أوصل للكنترول {profile.name} ❌ — الكنترول مردّش. "
                "اتأكد من الكابل والكونتاكت والتوصيلات وجرّب تاني."
            ),
            headline_en=(
                f"Couldn't reach {profile.name} ❌ — the ECU didn't answer. "
                "Check the cable, ignition and wiring, then try again."
            ),
            steps=_not_connected_steps(),
            identifiers=identifiers,
        )

    # If we actually read a live VIN, prefer it over the session VIN.
    if live is not None:
        for ident in live.identifiers:
            if ident.did == 0xF190 and ident.value:
                vin = ident.value
                break

    repo = pinout_repo or PinoutRepository()
    diagram = await repo.get(profile.name)
    # Variant profiles (e.g. FEM_F30_POST_2014) share a physical connector
    # with their base — fall back to the base diagram so the technician
    # still gets a pinout instead of a blank panel.
    if diagram is None and "_" in profile.name:
        base = "_".join(profile.name.split("_")[:2])  # FEM_F30
        if base != profile.name:
            diagram = await repo.get(base)

    callouts = list(diagram.callouts) if diagram else []
    open_module = profile.supports_software_only()

    if open_module:
        steps = _open_steps()
        wiring: list[WireConnection] = []
        boot = None
        cable = "enet"
        headline_ar = (
            f"تمام ✅ قريت السيارة. الكنترول {profile.name} مفتوح وتقدر "
            "تكوّد مباشرة بكابل الاي نت."
        )
        headline_en = (
            f"Read OK ✅ — {profile.name} is OPEN; you can code directly "
            "over the ENET cable."
        )
    else:
        wiring = _build_wiring(callouts)
        uses_can = any(w.function in ("canh", "canl") for w in wiring)
        boot = (profile.boot_pin if profile.boot_pin is not None
                else _find_pin(callouts, "boot", "bsl"))
        steps = _locked_steps(profile, wiring, boot, uses_can)
        cable = "dcan_bench"
        headline_ar = (
            f"قريت السيارة 🔒 الكنترول {profile.name} محمي ومحتاج bench + "
            "D-CAN. جهّز اللاب توب وكابل الـ D-CAN واتبع التوصيلات بالترتيب."
        )
        headline_en = (
            f"Read the car 🔒 — {profile.name} is protected and needs "
            "bench + D-CAN. Follow the wiring in order."
        )

    return ConnectAssessment(
        vin=vin,
        ecu_name=profile.name,
        chassis=chassis,
        engine=profile.engine,
        protection=ProtectionLevel(profile.protection).name,
        locked=not open_module,
        reachable=True,
        cable=cable,
        headline_ar=headline_ar,
        headline_en=headline_en,
        pinout_diagram_url=diagram.image_url if diagram else None,
        pinout_callouts=callouts,
        wiring=wiring,
        boot_pin=boot,
        steps=steps,
        identifiers=identifiers,
    )
