"""
robot/workshop.py — the robot talks shop: wiring, parts, BMW and MINI upkeep.

The system has a repair coach already — Repair Atlas's «أسطى Mouss Tec»
(repair_atlas/services/repair_coach.py): a workshop master's way of talking,
its rules on honesty (no invented connector numbers, torques or manuals), the
shop's slang («مكنة الإزاز» isn't the engine), and the shop's verified
knowledge (`VerifiedKnowledge`). When what's said to the robot is about how a
car works or is fixed — not a part to sell — the robot answers with that
coach: the same expertise, said aloud — a few short sentences, a step at a
time, then «أكمّلك؟».

It answers with what the system's own bots know, as facts to rely on:
  • a fault code («P0301»، «كود 2A82» aside — OBD codes): the diagnostics
    catalog (diagnostics_catalog.DTCDefinition — what Smart Diagnostics reads),
    else bmw_ecu's own Arabic descriptions (bmw_ecu.scan.dtc_decoder);
  • an ECU's pins («فيشة الـ DME»): bmw_ecu's pinout diagrams (EcuPinoutDiagram);
  • the shop's verified answers (Repair Atlas's VerifiedKnowledge).

Everything else general («إيه أحسن زيت للصيف؟»، «الجو حر النهارده»، «إيه رأيك في
الكورة؟») is a discussion: the robot talks it through (`discussion_prompt`) —
useful, a few sentences, a question back — never «محتاج قطعة إيه؟».
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger("mouss_tec_core")

LONGEST = 700                      # letters said at once: four short sentences, not a lecture

# What talking shop sounds like (normalized: ة→ه, أ→ا…).
_TECH = re.compile(
    r"ضفير|ضفاير|وايرنج|وايرينج|wiring|سلك|اسلاك|فيشه|كونيكت|كونكتور|فيوز|ريليه|ريلاي|حساس|سنسور|"
    r"كود عطل|كود|ارور|error|لمبه|صيان|عمره|تغيير الزيت|غيار زيت|كل كام كيلو|عزم|تيربو|تربو|"
    r"جيربوكس|فتيس|دبرياج|فرامل|تيل|تكييف|فريون|ردياتير|رديتر|ترموستات|طرمب|طلمب|بوجيه|كويل|"
    r"بخاخ|انجكتور|دينامو|مارش|بطاري|برمج|كودنج|coding|تشخيص|ب[يت]سخن|سخونيه|ب[يت]هز|ب[يت]صفر|ب[يت]طبل|"
    r"ب[يت]سحب|ب[يت]رعش|ب[يت]فوت|ب[يت]قطع|ب[يت]طفي|عطل|اعطال|مشكله|بيتغير امتي|فاناس|vanos|valvetronic|فالفترونيك|"
    r"idrive|اي درايف|موتور|المكنه|جوان|سير|شكمان|اكزوست|عفشه|مساعدين|مقصات|كوتش|"
    r"اغير|افك|اركب|اصلح|يتغير|يتركب|يتفك|يتصلح|"          # doing it: «ازاي أغير فلتر الزيت؟»
    r"(?:^|\s)[pbcu]\d{4}(?:\s|$)|كمبيوتر|دي ام اي|dme|ecu")   # a fault code, an ECU
# Asking how, where, why, when — a technical question, even beside a part's name.
_HOW = re.compile(r"ازاي|فين|مكان|ليه|امتي|اعمل ايه|نعمل ايه|اركب|افك|اغير|يتغير|يتركب|يتفك|"
                  r"اعرف منين|معناه|يعني ايه|بيعمل ايه|شغلته|اشرح|فهمني|قولي")
# Asking how a thing is done — never where it's on the shelf («فين الفلتر؟»): asked before the stock.
_HOW_DONE = re.compile(r"ازاي|ليه|اعمل ايه|نعمل ايه|اركب|افك|اغير|يتغير|يتركب|يتفك|كل كام|بيتغير امتي|"
                       r"اعرف منين|معناه|يعني ايه|بيعمل ايه|شغلته|اشرح|فهمني")
# Asking for a part to buy: the shop's own answer, not a lesson.
_BUY = re.compile(r"(?:^|\s)(?:عندك|عندكم|موجود|متوفر|بكام|سعر|هات|هاتلي|عايز اشتري)(?:\s|$)")

_MODE = (("wiring", re.compile(r"ضفير|ضفاير|وايرنج|وايرينج|wiring|سلك|اسلاك|فيشه|كونيكت|كونكتور|فيوز|ريليه|"
                               r"pin|فولت|volt")),
         ("locate", re.compile(r"فين|مكان|مكانها|مكانه")),
         ("install", re.compile(r"اركب|يتركب|تركيب")),
         ("disassembly", re.compile(r"افك|يتفك|فك|اشيل|اغير|تغيير")))

_VOICE = (
    "\n\n=== إنت دلوقتي «{robot}»، روبوت محل{shop} لقطع غيار BMW و MINI، وبتتكلم بخبرة الأسطى ده ===\n"
    "• ردك هيتقال بصوت عالي في المحل: من 2 لـ 4 جمل قصيرة بالعامية المصرية، من غير أي رموز ولا نجوم "
    "ولا جداول ولا mermaid ولا أكواد ولا لينكات — تجاهل أي تعليمات فوق عن الرسم أو الدياجرام أو الصور.\n"
    "• خطوة أو اتنين بس في المرة، وبعدين اسأل «أكمّلك؟» أو سؤال توضيح واحد.\n"
    "• لون سلك أو رقم كونيكتور أو عزم: قوله بس لو متأكد منه للعربية دي، وإلا قول إنه يتأكد من الدياجرام.\n"
    "• ممنوع تذكر أي سعر. ولو سأل عن قطعة يشتريها، قوله يسألك عنها باسمها أو رقمها.\n"
    "• إنت اللي بتجاوب: متقولش إنك هتسأل حد من الموظفين.\n"
    "• لو الكلام دردشة عامة مش تقنية، رد عليه عادي بلطف واختصار."
)


# A fault code said or written: «P0301»، «p 0301»، «بي 0301».
_DTC = re.compile(r"(?<![a-z0-9])([pbcu])\s?-?\s?(\d{4})(?!\d)", re.I)


def fault_codes(text: str) -> list:
    """The OBD fault codes in `text` («P0301»), at most three."""
    from . import wakename

    said = wakename.normalize(text or "")
    return list(dict.fromkeys(f"{letter.upper()}{digits}" for letter, digits in _DTC.findall(said)))[:3]


def fault_facts(text: str) -> str:
    """What the system's diagnostics know of each fault code said: the catalog's definition, causes
    and steps (what Smart Diagnostics reads), else bmw_ecu's own Arabic line for it."""
    from . import services

    lines = []
    for code in fault_codes(text):
        found = services.lookup_fault_code(code)
        if found.get("found"):
            line = f"{code}: {found.get('description') or ''}"
            if found.get("details"):
                line += f" — {str(found['details'])[:300]}"
            if found.get("likely_causes"):
                line += " — الأسباب المحتملة: " + "، ".join(str(c) for c in found["likely_causes"][:4])
            if found.get("likely_parts"):
                line += " — قطع ممكن تكون السبب (أرقام OEM): " + "، ".join(found["likely_parts"][:4])
            if found.get("guided_steps"):
                line += " — خطوات الفحص: " + "، ".join(str(s) for s in found["guided_steps"][:4])
            lines.append(line)
            continue
        try:
            from bmw_ecu.scan.dtc_decoder import _KNOWN, decode_dtc
            decoded = decode_dtc(code, 0x08)
            lines.append(f"{code}: {decoded.description_ar}"
                         + ("" if code in _KNOWN else " (ده وصف نوع العطل بس، مش الكود نفسه)"))
        except Exception as exc:
            logger.debug("robot workshop: no description for %s: %s", code, exc)
    return "\n".join(lines)


def pinout_facts(text: str) -> str:
    """An ECU named in `text` («فيشة الـ DME»، «MSD80»): its pins from bmw_ecu's pinout diagrams."""
    from django.db import transaction

    said = f" {(text or '').upper()} "
    try:
        from bmw_ecu.models import EcuPinoutDiagram

        with transaction.atomic():         # a schema without the table mustn't spoil the turn
            rows = list(EcuPinoutDiagram.objects.values_list("ecu_name", "callouts", "description")[:300])
    except Exception as exc:
        logger.debug("robot workshop: no pinouts: %s", exc)
        return ""
    lines = []
    for name, callouts, description in rows:
        if not name or not re.search(rf"(?<![A-Z0-9]){re.escape(name.upper())}(?![A-Z0-9])", said):
            continue
        pins = "، ".join(f"بن {c.get('pin')}: {c.get('label')}" for c in (callouts or [])[:12]
                        if isinstance(c, dict) and c.get("pin") is not None)
        lines.append(f"{name}: {pins or (description or '')[:300]}")
        if len(lines) >= 2:
            break
    return "\n".join(lines)


def is_technical(text: str) -> bool:
    """Whether `text` talks shop (how a car works or is fixed) rather than asking for a part."""
    from . import wakename

    said = wakename.normalize(text or "")
    if not _TECH.search(said):
        return False
    return bool(_HOW.search(said)) or not _BUY.search(said)


def asks_how(text: str) -> bool:
    """Talking shop and asking how a thing is done («ازاي أغير فلتر الزيت؟»): the coach answers it
    before the stock does — «أيوه، فلتر زيت موجود» isn't an answer to it."""
    from . import wakename

    return is_technical(text) and bool(_HOW_DONE.search(wakename.normalize(text or "")))


def mode_of(text: str) -> str:
    """The coach's mode for `text` («wiring», «locate», «install», «disassembly»), or ''."""
    from . import wakename

    said = wakename.normalize(text or "")
    return next((mode for mode, words in _MODE if words.search(said)), "")


def _vehicle(car: dict) -> dict:
    make = str((car or {}).get("make") or "").strip()
    models = [str(m) for m in (car or {}).get("models") or [] if m]
    return {"brand": make, "model_name": models[0] if models else ""}


def verified(text: str, car: dict) -> str:
    """The shop's verified answer near `text` (Repair Atlas's knowledge), or ''."""
    from django.db import transaction

    mode = mode_of(text) or "disassembly"
    vehicle = _vehicle(car)
    if not vehicle["brand"]:
        return ""
    try:
        from repair_atlas.services.repair_coach import _lookup_verified_kb

        with transaction.atomic():         # a schema without the table mustn't spoil the turn
            hit = _lookup_verified_kb(text, mode, vehicle)
    except Exception as exc:
        logger.debug("robot workshop: no verified knowledge: %s", exc)
        return ""
    return spoken(getattr(hit, "answer_markdown", "") or "", longest=1500) if hit else ""


def system_prompt(text: str, car: dict, *, robot: str = "موس", shop: str = "") -> str:
    """The coach's own system prompt, its mode for `text`, the car, then how to say it aloud."""
    from repair_atlas.services.repair_coach import _MODE_HINTS, _SYSTEM_BASE

    mode = mode_of(text)
    prompt = _SYSTEM_BASE + (f"\n\n=== سياق السؤال الحالي ===\n{_MODE_HINTS[mode]}" if mode else "")
    vehicle = _vehicle(car)
    if vehicle["brand"] or vehicle["model_name"]:
        prompt += (f"\n\n🚗 عربية العميل (سياق بس): {vehicle['brand']} {vehicle['model_name']}").rstrip()
    known = verified(text, car)
    if known:
        prompt += "\n\n📗 معلومة معتمدة من قاعدة معرفة الورشة — اعتمد عليها هي:\n" + known
    faults = fault_facts(text)
    if faults:
        prompt += ("\n\n📟 من كتالوج الأعطال عندنا (Smart Diagnostics) — اشرح الكود ده بيها، وقول أول "
                   "حاجة يتفحص إيه:\n" + faults)
    pins = pinout_facts(text)
    if pins:
        prompt += "\n\n🔌 من خرايط الفيش عندنا (BMW ECU) — أرقام البنات دي معتمدة:\n" + pins
    return prompt + _VOICE.format(robot=(robot or "موس").strip(), shop=f" «{shop}»" if shop else "")


_DISCUSS = (
    "إنت «{robot}»، روبوت محل{shop} لقطع غيار BMW و MINI في مصر، وبتتكلم بالعامية المصرية بصوت عالي.\n"
    "اللي قدامك عايز يدردش أو يتناقش في موضوع عام (عربيات، صيانة، كورة، الجو، الشغل، أي حاجة).\n"
    "• ناقشه بجد: رأي أو معلومة مفيدة وحقيقية، من 2 لـ 5 جمل قصيرة، وبعدين سؤال واحد يكمّل بيه الكلام.\n"
    "• افتكر اللي اتقال قبل كده في الكلام وكمّل عليه، متبدأش من الأول.\n"
    "• من غير أي رموز ولا نجوم ولا لينكات ولا قوايم — ده هيتقال بصوت.\n"
    "• ممنوع تذكر أسعار أو توعد بحاجة (خصم، توصيل، توفر قطعة). لو سأل عن قطعة يشتريها قوله يسألك عنها باسمها.\n"
    "• متقولش إنك هتسأل حد من الموظفين، ومتقولش إنك ذكاء اصطناعي إلا لو سأل.\n"
    "• لو مش متأكد من معلومة قول كده بصراحة."
)
DISCUSS_LONGEST = 600


def discussion_prompt(*, robot: str = "موس", shop: str = "") -> str:
    """The robot talking a general subject through: useful, a few sentences, a question back."""
    return _DISCUSS.format(robot=(robot or "موس").strip(), shop=f" «{shop}»" if shop else "")


_BLOCK = re.compile(r"```.*?(?:```|$)", re.S)
_MARKS = re.compile(r"[*#_`>|]+|^\s*[-•]\s*", re.M)


def spoken(text: str, longest: int = LONGEST) -> str:
    """`text` as it can be said: no code blocks, no markdown, cut at a sentence end."""
    said = _MARKS.sub(" ", _BLOCK.sub(" ", text or ""))
    said = re.sub(r"\s+", " ", said).strip()
    if len(said) <= longest:
        return said
    cut = said[:longest]
    end = max(cut.rfind(mark) for mark in (".", "؟", "?", "!", "،"))
    return (cut[:end + 1] if end > longest // 2 else cut).strip()
