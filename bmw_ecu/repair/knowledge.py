"""Curated repair knowledge base — the AI's grounding.

The AI Repair Assistant does NOT free-associate fixes. It reasons over a
declarative catalog of `RepairEntry` rows, each one a workshop-validated
link between *evidence* (a DTC, a symptom keyword, a live-data anomaly)
and a *cause + fix*. The Generator may only propose hypotheses that point
at an entry in here; the Verifier checks the proposed evidence against the
entry's expected evidence. That grounding is what makes the loop
self-checking instead of hallucination-prone — a hypothesis with no KB
backing scores zero and is dropped.

Each entry carries a `base_confidence` (a prior: how often, in this
workshop's experience, this cause is the real culprit when its triggers
fire) and the `confirm_pids` whose out-of-range live values would
*corroborate* it — the Verifier uses those to raise or lower confidence.

Adding workshop know-how later = adding a row here, no code change.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class RepairEntry:
    key: str
    cause_ar: str
    cause_en: str
    fix_ar: str
    fix_en: str
    # Evidence that activates this entry.
    trigger_dtcs: tuple[str, ...] = ()
    trigger_symptoms: tuple[str, ...] = ()      # lower-case AR/EN keywords
    # Live-data PIDs (see scan.live_data) whose ANOMALY corroborates the
    # cause; if such a PID is present but *in range* it weakly refutes it.
    confirm_pids: tuple[str, ...] = ()
    parts: tuple[str, ...] = ()
    base_confidence: float = 0.5                # 0..1 prior
    needs_safety_note: bool = False             # brakes/airbag/steering

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "cause_ar": self.cause_ar,
            "cause_en": self.cause_en,
            "fix_ar": self.fix_ar,
            "fix_en": self.fix_en,
            "trigger_dtcs": list(self.trigger_dtcs),
            "trigger_symptoms": list(self.trigger_symptoms),
            "confirm_pids": list(self.confirm_pids),
            "parts": list(self.parts),
            "base_confidence": round(self.base_confidence, 2),
            "needs_safety_note": self.needs_safety_note,
        }


# ─────────────────────────────────────────────────────────────────────
# The catalog. DTC strings line up with scan.dtc_decoder._KNOWN; PID codes
# line up with scan.live_data.PID_CATALOG.
# ─────────────────────────────────────────────────────────────────────
KNOWLEDGE_BASE: dict[str, RepairEntry] = {
    e.key: e for e in [
        # ── Misfire family ────────────────────────────────────────────
        RepairEntry(
            key="coil_pack_failure",
            cause_ar="بوبينة (Ignition Coil) ضعيفة في السلندر اللي بيخبط.",
            cause_en="Weak ignition coil on the misfiring cylinder.",
            fix_ar="غيّر بوبينة السلندر المحدد وافحص البوجيهات معاها.",
            fix_en="Replace the cylinder's coil; inspect its spark plug.",
            trigger_dtcs=("P0301", "P0302", "P0303", "P0304", "P0300"),
            trigger_symptoms=("misfire", "خبط", "رجة", "rough idle",
                              "هزة", "رعشة"),
            confirm_pids=("rpm", "lambda_b1"),
            parts=("ignition_coil", "spark_plug"),
            base_confidence=0.62,
        ),
        RepairEntry(
            key="spark_plug_worn",
            cause_ar="بوجيهات (Spark Plugs) قديمة/مفلجة واسعة.",
            cause_en="Worn or wide-gapped spark plugs.",
            fix_ar="غيّر طقم البوجيهات بالمواصفة الصح وظبّط الفلجة.",
            fix_en="Replace the full plug set with the correct gap.",
            trigger_dtcs=("P0300", "P0301", "P0302", "P0303", "P0304"),
            trigger_symptoms=("misfire", "خبط", "rough idle", "هزة",
                              "بطء عزم", "سحب ضعيف"),
            parts=("spark_plug",),
            base_confidence=0.5,
        ),
        # ── Lean / fuel-trim family ───────────────────────────────────
        RepairEntry(
            key="vacuum_leak",
            cause_ar="هواء داخل بعد المقياس (Vacuum Leak) فالخليط فقير.",
            cause_en="Unmetered air (vacuum/intake leak) leaning the mix.",
            fix_ar="افحص خراطيم السحب وجوان المنيفولد بدخان (smoke test).",
            fix_en="Smoke-test intake hoses + manifold gasket for leaks.",
            trigger_dtcs=("P0171", "P0174"),
            trigger_symptoms=("lean", "فقير", "rough idle", "سلنتيه واطية",
                              "تقطيع"),
            confirm_pids=("stft_b1", "ltft_b1", "maf"),
            base_confidence=0.55,
        ),
        RepairEntry(
            key="maf_dirty",
            cause_ar="حساس الهواء (MAF) متّسخ/بيقرأ غلط.",
            cause_en="Contaminated / mis-reading MAF sensor.",
            fix_ar="نظّف الـ MAF ببخاخه المخصص، ولو فضل غلط غيّره.",
            fix_en="Clean the MAF with MAF cleaner; replace if still off.",
            trigger_dtcs=("P0171", "P0174"),
            trigger_symptoms=("lean", "فقير", "hesitation", "تردد",
                              "ضعف عزم"),
            confirm_pids=("maf", "stft_b1", "ltft_b1"),
            parts=("maf_sensor",),
            base_confidence=0.45,
        ),
        # ── Catalyst ──────────────────────────────────────────────────
        RepairEntry(
            key="catalyst_aged",
            cause_ar="كفاءة الحفّاز (Catalyst) نزلت تحت الحد.",
            cause_en="Catalytic converter efficiency below threshold.",
            fix_ar="أكّد بحساس الأكسجين الخلفي ثم غيّر الحفّاز لو مات.",
            fix_en="Confirm via rear O2 sensor, then replace the cat.",
            trigger_dtcs=("P0420",),
            trigger_symptoms=("catalyst", "حفاز", "ريحة كبريت", "سخونة"),
            confirm_pids=("lambda_b1",),
            parts=("catalytic_converter",),
            base_confidence=0.5,
        ),
        # ── EVAP ──────────────────────────────────────────────────────
        RepairEntry(
            key="evap_leak_cap",
            cause_ar="طبة الوقود (Fuel Cap) مش قافلة صح أو جوانها تالف.",
            cause_en="Loose / failed fuel cap seal causing EVAP leak.",
            fix_ar="اربط الطبة كويس وامسح الكود؛ لو رجع اعمل smoke test.",
            fix_en="Reseat/replace the cap, clear; smoke-test if it returns.",
            trigger_dtcs=("P0455", "P0442"),
            trigger_symptoms=("evap", "تبخير", "ريحة بنزين", "fuel smell"),
            parts=("fuel_cap",),
            base_confidence=0.4,
        ),
        # ── VANOS / timing ────────────────────────────────────────────
        RepairEntry(
            key="vanos_solenoid",
            cause_ar="سولينويد الـ VANOS متّسخ فالتوقيت بيتقدّم/يتأخر.",
            cause_en="Dirty VANOS solenoid skewing cam timing.",
            fix_ar="نظّف سولينويدات الـ VANOS وفلترها، أكّد ضغط الزيت.",
            fix_en="Clean VANOS solenoids + screens; verify oil pressure.",
            trigger_dtcs=("P0011", "P0012", "P0014"),
            trigger_symptoms=("vanos", "فانوس", "rough idle", "ضعف عزم تحت"),
            confirm_pids=("oil_temp", "rpm"),
            parts=("vanos_solenoid",),
            base_confidence=0.5,
        ),
        # ── Cooling ───────────────────────────────────────────────────
        RepairEntry(
            key="thermostat_stuck_open",
            cause_ar="الثرموستات سايب مفتوح فالموتور مابيسخنش.",
            cause_en="Thermostat stuck open — engine under-heats.",
            fix_ar="غيّر الثرموستات (الإلكتروني لو موجود) وافحص الحساس.",
            fix_en="Replace the (map-controlled) thermostat; check sensor.",
            trigger_dtcs=("P0128",),
            trigger_symptoms=("overheat", "تبريد", "حرارة واطية",
                              "السخانة بتبرد", "cold"),
            confirm_pids=("coolant_temp",),
            parts=("thermostat",),
            base_confidence=0.6,
        ),
        # ── Transmission ──────────────────────────────────────────────
        RepairEntry(
            key="trans_mechatronic",
            cause_ar="ميكاترونيك الجير (Mechatronic) أو سولينويداته.",
            cause_en="Transmission mechatronic / solenoid fault.",
            fix_ar="اقرأ أكواد الجير، غيّر زيت+فلتر الميكاترونيك أولاً.",
            fix_en="Read trans codes; service mechatronic oil+filter first.",
            trigger_dtcs=("P0700", "P0730"),
            trigger_symptoms=("gear", "جير", "نقلات", "خبطة في النقل",
                              "limp", "وضع الطوارئ"),
            parts=("trans_oil_filter",),
            base_confidence=0.45,
        ),
        # ── Network / comms ───────────────────────────────────────────
        RepairEntry(
            key="lost_comm_power_ground",
            cause_ar="فقد اتصال وحدة على الـ CAN — غالباً تغذية/أرضي.",
            cause_en="Module lost CAN comms — usually power/ground/wiring.",
            fix_ar="افحص فيش وتغذية وأرضي الوحدة قبل ما تتهمها.",
            fix_en="Check the module's connector, power + ground first.",
            trigger_dtcs=("U0100", "U0101", "U0121"),
            trigger_symptoms=("no communication", "مفيش اتصال",
                              "وحدة مش بترد", "lost comm"),
            confirm_pids=("battery_voltage",),
            base_confidence=0.5,
        ),
        # ── Battery / charging ────────────────────────────────────────
        RepairEntry(
            key="weak_battery",
            cause_ar="بطارية ضعيفة/مش متسجلة فبتدي أكواد عشوائية.",
            cause_en="Weak/unregistered battery causing spurious faults.",
            fix_ar="اختبر البطارية، سجّلها (CBS) لو اتغيرت، واشحن.",
            fix_en="Test the battery; register it (CBS) if new; charge.",
            trigger_symptoms=("battery", "بطارية", "تقفيل", "بطيء التدوير",
                              "crank slow", "أكواد كتير"),
            confirm_pids=("battery_voltage",),
            parts=("battery",),
            base_confidence=0.4,
        ),
        # ── Restraint (safety) ────────────────────────────────────────
        RepairEntry(
            key="crash_data_stored",
            cause_ar="بيانات تصادم متخزّنة فوحدة الإيرباج (ACSM).",
            cause_en="Crash data stored in the restraint module (ACSM).",
            fix_ar="افحص حالة الأكياس والشدّادات أولاً، بعدها امسح بيانات "
                   "التصادم بخدمة ACSM المعتمدة.",
            fix_en="Inspect bags + pretensioners FIRST, then clear crash "
                   "data via the approved ACSM service.",
            trigger_dtcs=("B1018",),
            trigger_symptoms=("airbag", "إيرباج", "كرسي مكسور", "حادثة"),
            base_confidence=0.7,
            needs_safety_note=True,
        ),
        # ── Brakes / ABS (safety) ─────────────────────────────────────
        RepairEntry(
            key="wheel_speed_sensor",
            cause_ar="حساس سرعة عجلة (ABS) تالف أو ترسه متآكل.",
            cause_en="Faulty ABS wheel-speed sensor or damaged tone ring.",
            fix_ar="افحص الحساس وترس النغمة، غيّر التالف وامسح الكود.",
            fix_en="Inspect sensor + tone ring; replace the faulty side.",
            trigger_dtcs=("C1234",),
            trigger_symptoms=("abs", "لمبة فرامل", "تحكم بالثبات",
                              "wheel speed"),
            parts=("wheel_speed_sensor",),
            base_confidence=0.55,
            needs_safety_note=True,
        ),
    ]
}


def _norm(s: str) -> str:
    return s.strip().lower()


def entries_for_dtc(code: str) -> tuple[RepairEntry, ...]:
    """Every KB entry whose triggers include this DTC code."""
    c = code.strip().upper()
    return tuple(e for e in KNOWLEDGE_BASE.values() if c in e.trigger_dtcs)


def entries_for_symptom(text: str) -> tuple[RepairEntry, ...]:
    """KB entries whose symptom keywords appear in the free-text phrase."""
    t = _norm(text)
    if not t:
        return ()
    hits = []
    for e in KNOWLEDGE_BASE.values():
        if any(kw and kw in t for kw in e.trigger_symptoms):
            hits.append(e)
    return tuple(hits)


def get_entry(key: str) -> RepairEntry | None:
    return KNOWLEDGE_BASE.get(key)


def all_entries() -> tuple[RepairEntry, ...]:
    return tuple(KNOWLEDGE_BASE.values())
