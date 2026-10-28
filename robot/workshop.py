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

Everything else general («إيه أحسن زيت للصيف؟»، «الجو حر النهارده») is the
robot's own small talk (services.ai_reply), allowed to be useful too.
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
    r"اغير|افك|اركب|اصلح|يتغير|يتركب|يتفك|يتصلح")          # doing it: «ازاي أغير فلتر الزيت؟»
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
    return prompt + _VOICE.format(robot=(robot or "موس").strip(), shop=f" «{shop}»" if shop else "")


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
