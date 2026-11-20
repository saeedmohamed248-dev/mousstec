"""🧠 خدمات الروبوت — الرد بالذكاء الاصطناعي على الأسئلة العامة والبحث على النت."""

from __future__ import annotations


import logging
import os
import re

from django.utils import timezone

from .. import language
from robot import services  # 🔌 seams: بتتنادى عن طريق الحزمة عشان الاختبارات تقدر تعمل لها patch
from .catalog import _ARABIC_DIGITS

logger = logging.getLogger("mouss_tec_core")


# ---------------------------------------------------------------------------
# Conversational fallback (LLM) — general questions the router doesn't cover
# ---------------------------------------------------------------------------

_AI_SYSTEM_PROMPT = (
    "أنت «{robot}»، روبوت مساعد في محل{shop} لقطع غيار BMW و MINI. رد بالعامية "
    "المصرية في جملة أو اتنين قصيرين لأن ردك هيتقال بصوت. ممنوع تذكر أي سعر أو "
    "تكلفة أو سعر جملة (غير سعر البيع اللي في بيانات القطعة لو اتبعتتلك)، وممنوع "
    "تخترع توفر قطعة أو رقمها: لو السؤال عن قطعة "
    "معينة قول إنك هتسأل حد من الموظفين أو اطلب رقم القطعة. ساعد في الأسئلة "
    "العامة عن الأعطال والصيانة والمواعيد بشكل عام. ولو الكلام دردشة عامة، اتكلم معاه "
    "عادي بلطف وادي معلومة مفيدة لو عندك، من غير ما توعد بحاجة. متوعدش بحاجة مش مكتوبة قدامك "
    "(إن القطع أصلية، خصم، توصيل، تركيب)."
)
# The answer is said while the customer waits: one model this long, all of them this long.
REPLY_TIMEOUT_S = 5
REPLY_BUDGET_S = 7
# 🐛 [FIX]: لما Gemini مردّش (حصته أو موديل بطيء) البوابة العامة كانت بتتسأل بعده وتاخد ٤ ثواني كمان
# وترجع فاضي: ١١–١٢ ثانية والزبون واقف. البوابة بس لو Gemini قال «لأ» بسرعة (مفيش مفتاح مثلاً).
GATEWAY_IF_UNDER_S = 2
_WEEKDAYS = ("الاتنين", "التلات", "الأربع", "الخميس", "الجمعة", "السبت", "الحد")


def _shop_and_time() -> dict:
    """What the prompts are filled with: the shop's own name, and «النهارده
    الحد، والساعة 4:10 العصر» — «الساعة كام؟» and «فاتحين دلوقتي؟» need it."""
    shop = services.shop_name()
    now = timezone.localtime()
    hour = now.hour % 12 or 12
    part = ("الصبح" if now.hour < 12 else "الضهر" if now.hour < 15
            else "العصر" if now.hour < 18 else "بالليل")
    return {"shop": f" «{shop}»" if shop else "",
            "now": f" النهارده {_WEEKDAYS[now.weekday()]} {now:%d/%m/%Y}، والساعة {hour}:{now:%M} {part}."}


_AI_ENGLISH = (
    " The customer spoke English: reply in short, natural spoken English "
    "instead, with the same rules."
)

# With the part being talked about in front of it (robot/memory.facts).
_AI_FACTS_PROMPT = (
    "\n\nدي بيانات القطعة اللي العميل بيتكلم عنها، من المخزون نفسه. اعتمد عليها "
    "بس: أي سعر تقوله لازم يكون «سعر البيع» المكتوب هنا بالظبط، ومتقولش أي رقم "
    "مش مكتوب. ولو حاجة مش مكتوبة قول إنك هتسأل حد من الموظفين. تقدر تقنع العميل "
    "بالحاجات الحقيقية اللي هنا بس (الضمان، الماركة، التوافق، إنها متوفرة) — من "
    "غير مبالغة ولا وعود ولا خصومات. ولو السؤال عن قطعة تانية، اطلب اسمها أو رقمها.\n"
)

_CURRENCY = re.compile(r"جنيه|جنية|جنيهات|ج\.م|قرش|pounds?|egp|\ble\b", re.IGNORECASE)
_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")


def invented_price(reply: str, allowed: str) -> bool:
    """True when `reply` says a sum of money that isn't in `allowed` — the
    part's own data and what the customer said. A price the robot speaks is
    only ever the catalog's."""
    text = (reply or "").translate(_ARABIC_DIGITS)
    known = {n.replace(",", "") for n in _NUMBER.findall((allowed or "").translate(_ARABIC_DIGITS))}
    for m in _NUMBER.finditer(text):
        near = text[max(0, m.start() - 12):m.end() + 14]
        if _CURRENCY.search(near) and m.group().replace(",", "") not in known:
            return True
    return False


def ai_reply(transcript: str, lang: str = "ar", *, facts: str = "", history=(), branch: str = "",
             robot: str = "موس", system: str = "", longest: int = 400) -> str:
    """A short spoken answer from the ERP's LLM gateway, or '' if unavailable.

    Used only when no rule matched. In Egyptian Arabic, or in English when the
    customer spoke English (`lang`). It sees what was just said (`history`:
    [said, replied] pairs, robot/memory.py) and the part being talked about
    (`facts`), so «وده بيعمل إيه؟» is about that part. The reply is scrubbed
    with `redact` so it can never read out a wholesale/cost figure, and
    dropped if it names a price that isn't the part's own.

    `system`: another prompt in place of the robot's own (robot/workshop.py: the repair
    coach, said aloud); `longest`: letters said at most.
    """
    from ..pricing import redact
    text = (transcript or "").strip()
    if not text:
        return ""
    # «انت في أنهي فرع؟», «ده فرع إيه؟» — however it's asked.
    place = f" وانت في فرع {branch}." if branch else ""
    filled = _shop_and_time()
    # 🐛 [FIX]: «اسمه Fixit» بعد «محل» اتفهم إنه اسم الروبوت: «أنا فكس إت من الفرع الرئيسي». الروبوت
    # ليه اسمه («موس»)، والمحل اسمه قبل نوعه.
    system = ((system or _AI_SYSTEM_PROMPT.format(shop=filled["shop"], robot=(robot or "موس").strip()))
              + place + filled["now"]
              + (_AI_FACTS_PROMPT + facts if facts else "") + (_AI_ENGLISH if lang == "en" else ""))
    turns = [(str(said)[:200], str(replied)[:300]) for said, replied in list(history or [])[-4:]]
    # 🐛 [FIX]: «بكام سعر الياباني؟» استنى 10 ثواني: بوابة الـ LLM العامة (Together) كانت بتتسأل
    # الأول وGemini بعدها. دلوقتي Gemini الأول (المفتاح اللي المحل مشترك فيه)، بمهلة قصيرة —
    # الرد بيتقال بصوت والزبون واقف — والبوابة بس لو Gemini مردّش.
    import time

    from ..audio import gemini_generate
    said_before = "".join(f"العميل: {s}\nانت: {r}\n" for s, r in turns)
    parts = [{"text": system}] + ([{"text": "اللي اتقال قبل كده:\n" + said_before}] if said_before else [])
    from .. import llm

    reply, took = "", None
    # Gemini out of time a moment ago (robot/llm.py): not waited on again — the backup model answers.
    if not llm.gemini_down():
        started = time.monotonic()
        reply = gemini_generate(parts + [{"text": text[:500]}], what="reply",
                                timeout=REPLY_TIMEOUT_S, budget=REPLY_BUDGET_S,
                                prefer=os.getenv("ROBOT_REPLY_MODEL", "").strip())
        took = time.monotonic() - started
        llm.note_gemini(bool(reply), took)
    if not reply and llm.has_backup():
        reply = llm.backup(system, text[:500], history=turns)
    elif not reply and took is not None and took < GATEWAY_IF_UNDER_S:
        messages = [{"role": "system", "content": system}]
        for said, replied in turns:
            messages += [{"role": "user", "content": said}, {"role": "assistant", "content": replied}]
        messages.append({"role": "user", "content": text[:500]})
        try:
            from inventory.ai_services import call_llm_layer
            reply = (call_llm_layer(messages, max_retries=1) or "").strip()
        except Exception:
            reply = ""
    if not reply:
        return ""
    # 🐛 [FIX]: from the shop's log — «Could you please confirm the exact model year…» said to an
    # Egyptian customer. An Arabic question's answer in English isn't said.
    if lang != "en" and language.detect(reply) == "en":
        logger.warning("robot ai_reply: dropped an English answer to an Arabic question")
        return ""
    reply = redact(reply[:longest])
    if invented_price(reply, facts + "\n" + text):
        logger.warning("robot ai_reply: dropped an answer with a price not in the catalog")
        return ""
    return reply


# What the shop's people ask that the ERP doesn't know: the internet.
_WEB_PROMPT = (
    "أنت روبوت مساعد في محل{shop} لقطع غيار BMW و MINI، وبتكلم حد من إدارة "
    "المحل. دوّر على النت على اللي بيسأل عنه، ورد بالعامية المصرية في جملتين أو تلاتة "
    "قصيرين لأن ردك هيتقال بصوت: المعلومة نفسها بس، من غير لينكات ولا علامات ولا "
    "مقدمات. لو ملقتش حاجة أكيدة قول كده بصراحة. أي سعر لقيته قول إنه سعر على النت "
    "مش سعر المحل."
)
_MARKUP = re.compile(r"\[\d+(?:,\s*\d+)*\]|[*#_`]+")


def web_answer(question: str, lang: str = "ar", *, facts: str = "", history=()) -> tuple:
    """(a short spoken answer, the pages it came from) — Gemini looking it
    up with Google Search. ('', []) when it can't. Only for the staff who may
    (robot/assistant.web_reply): it's the internet, not the ERP."""
    from ..audio import gemini_generate
    from ..pricing import redact

    text = (question or "").strip()
    if not text:
        return "", []
    filled = _shop_and_time()
    system = (_WEB_PROMPT.format(shop=filled["shop"]) + filled["now"]
              + ("\n\nالقطعة اللي بنتكلم عنها، من المخزون:\n" + facts if facts else "")
              + (_AI_ENGLISH if lang == "en" else ""))
    said_before = "".join(f"هو: {str(s)[:200]}\nانت: {str(r)[:300]}\n" for s, r in list(history or [])[-4:])
    parts = [{"text": system}] + ([{"text": "اللي اتقال قبل كده:\n" + said_before}] if said_before else [])
    sources: list = []
    reply = gemini_generate(parts + [{"text": text[:500]}], what="web search",
                            tools=[{"google_search": {}}], sources=sources)
    reply = re.sub(r"\s+([.،,!؟?:])", r"\1", " ".join(_MARKUP.sub(" ", reply or "").split()))
    if not reply:
        return "", []
    return redact(reply[:500]), sources
