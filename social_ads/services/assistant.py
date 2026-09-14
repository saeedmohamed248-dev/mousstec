"""
مساعد التسويق الذكي — a conversational studio assistant.

The shop owner talks to this the way they'd talk to any AI chat: in plain
Egyptian Arabic they can give commands ("نزّل بوست مشاعر دلوقتي"), ask for advice
("إيه أحسن نوع محتوى أنزّله الأسبوع ده؟"), or paste a competitor's post and ask
for an analysis. The assistant has the tenant's FULL studio context — brand
profile, learned strategy brief, KPIs, best/selling angles, live inventory
snapshot, attributed sales — so its advice is grounded in real data, and it can
EXECUTE the same actions the studio buttons trigger.

Design:
  • build_context(config)      → a compact Arabic snapshot of everything the bot
                                 knows about this business.
  • chat(config, message, hist)→ ask the LLM to (a) reply/advise in Egyptian
                                 Arabic and (b) optionally pick ONE action to run,
                                 returned as strict JSON {reply, action, params}.
  • execute(config, action,..) → dispatch the chosen action to the existing tasks
                                 and return a short human confirmation.

Everything degrades gracefully: an LLM hiccup returns a helpful fallback reply
with action="none" (never raises), so the chat box always answers.
"""
from __future__ import annotations

import logging
from typing import Optional

from django.db.models import Avg, Sum

from . import catalog, content_ai, strategist

logger = logging.getLogger("mouss_tec_core")

# Actions the assistant is allowed to trigger from a chat command. Each maps to a
# studio capability that already exists as a Celery task / view.
ACTIONS = {
    "generate_ideas",     # params: {count, content_type, image_source}
    "generate_post",      # params: {angle, content_type, occasion, image_source} → one post
    "analyze_page",       # import + learn from the real page (up to 1000 posts)
    "autopost_inventory",  # params: {count, strategy} → product posts from inventory
    "learn",              # re-run the learning cycle now
    "weekly_report",      # (handled in the view: renders the report screen)
    "ab_test",            # params: {angle, occasion}
    "analyze_competitor",  # params: {text} → pure analysis, no publishing
    "none",               # advice / answer only, nothing to run
}

_CONTENT_TYPES = ("mix", "tips", "emotional", "product", "engagement")


# =====================================================================
# Context — what the bot knows about this business
# =====================================================================
def build_context(config) -> str:
    """A compact Arabic snapshot fed to the assistant so its advice is grounded."""
    from social_ads.models import SocialPost

    lines: list[str] = []
    lines.append("=== ملف النشاط ===")
    lines.append(content_ai._brand_block(config))

    # KPIs from published posts.
    published = SocialPost.objects.filter(config=config, status=SocialPost.Status.PUBLISHED)
    pub_count = published.count()
    avg_er = round(published.aggregate(a=Avg("engagement_rate"))["a"] or 0.0, 2)
    total_reach = published.aggregate(s=Sum("reach"))["s"] or 0
    attributed_value = published.aggregate(s=Sum("attributed_sales_value"))["s"] or 0
    drafts = SocialPost.objects.filter(config=config, status=SocialPost.Status.DRAFT).count()
    scheduled = SocialPost.objects.filter(config=config, status=SocialPost.Status.SCHEDULED).count()

    lines.append("")
    lines.append("=== أرقام الأداء الحالية ===")
    lines.append(f"بوستات منشورة: {pub_count} | مجدولة: {scheduled} | مسودات: {drafts}")
    lines.append(f"متوسط معدل التفاعل: {avg_er}% | إجمالي الوصول: {int(total_reach):,}")
    if attributed_value:
        lines.append(f"مبيعات منسوبة للبوستات: {float(attributed_value):,.0f}")

    # Learned strategy.
    memory = strategist.ensure_memory(config)
    if memory and memory.learned_brief:
        lines.append("")
        lines.append("=== ما تعلّمه البوت من أداء صفحتك (خلاصة الاستراتيجية) ===")
        lines.append(memory.learned_brief.strip()[:1200])
    best_selling = memory.best_selling_angles(3) if memory else []
    best_eng = memory.best_angles(3) if memory else []
    if best_selling:
        lines.append(f"أكثر زوايا المحتوى بيعاً: {', '.join(best_selling)}")
    if best_eng:
        lines.append(f"أكثر زوايا المحتوى تفاعلاً: {', '.join(best_eng)}")
    if memory and memory.best_hours:
        lines.append(f"أفضل أوقات النشر: {', '.join(memory.best_hours)}")

    # Audience knowledge base — what customers actually ask in the comments.
    try:
        from . import audience
        voice = audience.summarize_for_context(getattr(memory, "audience_insights", None) or {})
    except Exception:
        voice = ""
    if voice:
        lines.append("")
        lines.append("=== صوت العملاء (من تعليقات صفحتك) ===")
        lines.append(voice)

    # Inventory snapshot (a few real products the bot could promote).
    try:
        products = catalog.fetch_catalog_products(config, count=6, require_image=False)
    except Exception:
        products = []
    if products:
        lines.append("")
        lines.append("=== عيّنة من المخزون الحقيقي (منتجات ممكن تروّجها) ===")
        for p in products[:6]:
            price = p.get("price") or 0
            price_txt = f"{price:,.0f}" if price else "السعر عند الطلب"
            car = f" — {p.get('car_model','')} {p.get('car_year','')}".rstrip()
            lines.append(f"- {p.get('name','')} ({p.get('brand','')}) {price_txt}{car}")

    return "\n".join(lines)


# =====================================================================
# Chat — reply + optional action
# =====================================================================
_SYSTEM = (
    "إنت مساعد تسويق ذكي وخبير لمحل قطع غيار مصري (زي مدير تسويق شاطر بيشتغل مع صاحب المحل). "
    "بتتكلم عامية مصرية بسيطة وودّية، من غير أي أسلوب روبوت أو كلام رسمي جامد. "
    "عندك كل بيانات النشاط والأداء والمخزون تحت (اقرأها كويس واستخدمها في نصايحك). "
    "دورك:\n"
    "1) لو صاحب المحل سألك نصيحة أو رأي أو سؤال — رُدّ برأي عملي محدد مبني على أرقامه هو "
    "(مش كلام عام)، وقوله بالظبط يعمل إيه ولية.\n"
    "2) لو طلب منك تنفّذ حاجة (زي: نزّل بوست مشاعر، ولّد ١٠ أفكار، حلّل صفحتي، اعمل بوستات من "
    "المخزون، اتعلّم من الأداء، اعمل تجربة A/B، اعملي تقرير) — اختار الأكشن المناسب ونفّذه.\n"
    "3) لو لصق كلام منافس وطلب تحليل — حلّله (نقاط قوته/ضعفه وإزاي نتفوّق عليه) بدون ما تنشر حاجة.\n"
    "\n"
    "لازم ترد بصيغة JSON بالظبط كده (من غير أي كلام قبلها أو بعدها):\n"
    '{"reply": "ردّك بالعامية المصرية للمستخدم", '
    '"action": "<واحد من: generate_ideas | generate_post | analyze_page | autopost_inventory | learn | weekly_report | ab_test | analyze_competitor | none>", '
    '"params": {}}\n'
    "\n"
    "قواعد الأكشن:\n"
    "- generate_post: بوست واحد. params ممكن تحتوي content_type (mix/tips/emotional/product/engagement) "
    "أو angle، و occasion لو فيه مناسبة، و image_source (inventory/ai/none). "
    "مثال: 'نزّل بوست مشاعر' → content_type='emotional'.\n"
    "- generate_ideas: مجموعة أفكار/مسودات. params: count (١-٢٥)، content_type، image_source.\n"
    "- autopost_inventory: بوستات منتجات من المخزون. params: count (١-١٠)، strategy (new/featured/low).\n"
    "- analyze_page: يستورد بوستات الصفحة الحقيقية ويتعلّم منها.\n"
    "- learn: يعيد تحليل الأداء ويحدّث الاستراتيجية.\n"
    "- weekly_report: يفتح التقرير الأسبوعي.\n"
    "- ab_test: تجربة نسختين. params: angle، occasion.\n"
    "- analyze_competitor: params: text (كلام المنافس). التحليل يبقى جوه reply نفسه.\n"
    "- none: لو مفيش تنفيذ مطلوب (نصيحة/إجابة بس).\n"
    "\n"
    "في reply لما تنفّذ أكشن، اكتب إنك بدأت تعمله (مثلاً 'تمام، بجهّزلك بوست مشاعر دلوقتي…') "
    "وضيف نصيحة قصيرة لو ينفع. خلّي reply مختصر ومفيد ومن غير رموز JSON جوّاه."
)


def chat(config, message: str, history: Optional[list] = None) -> dict:
    """Return {reply, action, params}. Never raises."""
    message = (message or "").strip()
    if not message:
        return {"reply": "اكتبلي إنت عايز إيه 🙂", "action": "none", "params": {}}

    context = build_context(config)
    convo = _format_history(history)
    user_msg = (
        f"{context}\n\n"
        f"{convo}"
        f"=== رسالة صاحب المحل دلوقتي ===\n{message}\n\n"
        "ردّ بصيغة JSON بس زي ما اتشرح."
    )

    raw = None
    try:
        raw = content_ai._generate(config, _SYSTEM, user_msg, max_tokens=1200)
    except Exception:
        logger.warning("social_ads assistant: LLM call failed for %s",
                       config.tenant.schema_name, exc_info=True)

    parsed = content_ai._parse_json(raw) if raw else None
    if not parsed or not parsed.get("reply"):
        # LLM unavailable (quota/outage) — stay useful WITHOUT it: run the command
        # from keywords, or answer data questions straight from the tenant's data.
        return _offline_reply(config, message)

    action = (parsed.get("action") or "none").strip()
    if action not in ACTIONS:
        action = "none"
    params = parsed.get("params") or {}
    if not isinstance(params, dict):
        params = {}
    return {
        "reply": str(parsed.get("reply") or "").strip(),
        "action": action,
        "params": params,
    }


def _format_history(history: Optional[list]) -> str:
    """Render recent turns as context (list of {role, text})."""
    if not history:
        return ""
    lines = ["=== آخر رسائل في المحادثة ==="]
    for turn in history[-6:]:
        role = "صاحب المحل" if turn.get("role") == "user" else "المساعد"
        text = (turn.get("text") or "").strip()
        if text:
            lines.append(f"{role}: {text[:400]}")
    lines.append("")
    return "\n".join(lines)


# =====================================================================
# Offline mode — works even when the LLM (Gemini) quota is exhausted
# =====================================================================
def _offline_reply(config, message: str) -> dict:
    """Best-effort reply with NO LLM: rule-based command, then a data answer.

    Keeps the bot's core useful (run commands, answer questions from its own
    data) when the model is unavailable — so it never dead-ends the user.
    """
    # 1) Did they ask to RUN something? Detect the command from keywords.
    intent = _rule_based_intent(message)
    if intent:
        action, params = intent
        return {"reply": _ack_for(action, params), "action": action, "params": params}

    # 2) Is it a data question we can answer from stored data?
    answer = _answer_from_data(config, message)
    if answer:
        return {"reply": answer, "action": "none", "params": {}}

    # 3) Nothing matched → honest fallback that still points to what works.
    return {
        "reply": (
            "النظام الذكي مشغول شوية دلوقتي (وصلنا للحد اليومي المجاني للـ AI). "
            "بس لسه أقدر أنفّذلك أوامر — قوللي مثلاً:\n"
            "• «نزّل بوست مشاعر» أو «بوست نصايح» أو «بوست منتج»\n"
            "• «ولّد ١٠ أفكار»\n"
            "• «بوستات من المخزون»\n"
            "• «حلّل صفحتي» / «اتعلّم» / «التقرير الأسبوعي»\n"
            "أو اسألني: «كام بوست عندي؟» / «أنجح بوست» / «الناس بتسأل عن إيه؟»"
        ),
        "action": "none",
        "params": {},
    }


def _rule_based_intent(message: str):
    """Map a plain-Arabic command to (action, params) with keyword rules. None if
    the text isn't a clear command."""
    t = (message or "").lower()

    def has(*words):
        return any(w in t for w in words)

    # A number in the text → count. Normalise Arabic-Indic digits (٠-٩) to ASCII
    # first, since Egyptian users often type them.
    import re as _re
    _digits = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
    t_num = t.translate(_digits)
    num = None
    m = _re.search(r"\d+", t_num)
    if m:
        try:
            num = int(m.group(0))
        except ValueError:
            num = None

    if has("حلل صفحت", "حلّل صفحت", "تحليل صفحت", "استورد", "بوستاتي القديم"):
        return ("analyze_page", {})
    if has("تقرير"):
        return ("weekly_report", {})
    if has("اتعلم", "اتعلّم", "تعلم من", "حدّث الاستراتيج", "حدث الاستراتيج"):
        return ("learn", {})
    if has("من المخزون", "من مخزون", "بوستات منتجات", "بضاعت"):
        return ("autopost_inventory", {"count": min(num or 3, 10)})
    if has("a/b", "ab", "تجربة", "نسختين"):
        return ("ab_test", {})

    # Post generation by content type.
    ctype = None
    if has("مشاعر", "قصة", "قصص", "احاسيس", "أحاسيس"):
        ctype = "emotional"
    elif has("نصايح", "نصيحة", "نصائح", "معلومة", "معلومات"):
        ctype = "tips"
    elif has("منتج", "عرض", "سعر", "خصم"):
        ctype = "product"
    elif has("سؤال", "تفاعل", "استفتاء"):
        ctype = "engagement"

    wants_many = has("افكار", "أفكار", "فكرة", "فكره", "مجموعة", "كذا بوست") or (num and num >= 3)
    wants_post = has("بوست", "منشور", "انشر", "انزل", "نزّل", "نزل", "اكتب",
                     "اعمل", "ولّد", "ولد", "جهّز", "جهز", "هات", "عايز بوست", "عاوز بوست")

    if wants_many and (ctype or wants_post):
        return ("generate_ideas", {"count": min(num or 10, 25), "content_type": ctype or "mix"})
    if wants_post and ctype:
        return ("generate_post", {"content_type": ctype})
    if wants_post:
        return ("generate_post", {"content_type": "mix"})
    return None


def _ack_for(action: str, params: dict) -> str:
    ct = {"emotional": "مشاعر", "tips": "نصايح", "product": "منتج",
          "engagement": "تفاعلي", "mix": "متنوّع"}.get(params.get("content_type", ""), "")
    if action == "generate_post":
        return f"تمام 👍 بجهّزلك بوست {ct} دلوقتي… هيبان في المسودات بعد لحظات."
    if action == "generate_ideas":
        return f"تمام، بولّدلك {params.get('count', 10)} فكرة {ct}… راجعها في المسودات."
    if action == "autopost_inventory":
        return f"ماشي، بجهّز {params.get('count', 3)} بوست منتجات من مخزونك."
    if action == "analyze_page":
        return "تمام، ببدأ أحلّل صفحتك وأتعلّم من بوستاتها وتعليقاتها."
    if action == "learn":
        return "ماشي، بعيد تحليل الأداء وأحدّث الاستراتيجية."
    if action == "weekly_report":
        return "بفتحلك التقرير الأسبوعي 👇"
    if action == "ab_test":
        return "تمام، بجهّز تجربة A/B بنسختين."
    return "تمام، بنفّذ طلبك."


def _answer_from_data(config, message: str) -> str:
    """Answer common data questions straight from stored data — no LLM. None if
    the question isn't one we recognise."""
    from social_ads.models import SocialPost

    t = (message or "").lower()

    def has(*words):
        return any(w in t for w in words)

    # What do customers ask about? → audience insights.
    if has("العملاء", "الناس", "الجمهور", "بيسأل", "بيسألوا", "عايزين", "التعليقات", "الكومنت"):
        memory = strategist.ensure_memory(config)
        from . import audience
        voice = audience.summarize_for_context(getattr(memory, "audience_insights", None) or {})
        if voice:
            return "أهم اللي طالع من تعليقات عملائك:\n\n" + voice
        return ("لسه معنديش تعليقات محلّلة كفاية. دوس «حلّل صفحتي» الأول عشان "
                "أقرأ تعليقات صفحتك وأتعلّم منها.")

    # How many posts / stats?
    if has("كام بوست", "عدد البوست", "كام منشور", "احصائيات", "إحصائيات", "الأرقام", "الارقام"):
        pub = SocialPost.objects.filter(config=config, status=SocialPost.Status.PUBLISHED).count()
        dr = SocialPost.objects.filter(config=config, status=SocialPost.Status.DRAFT).count()
        sc = SocialPost.objects.filter(config=config, status=SocialPost.Status.SCHEDULED).count()
        return f"عندك {pub} بوست منشور، {sc} مجدول، و{dr} مسودة."

    # Best post?
    if has("أنجح", "انجح", "أفضل بوست", "احسن بوست", "أحسن بوست"):
        best = (SocialPost.objects.filter(config=config, status=SocialPost.Status.PUBLISHED)
                .order_by("-engagement_rate", "-likes").first())
        if best:
            cap = (best.caption or "")[:120]
            return (f"أنجح بوست عندك (تفاعل {best.engagement_rate}% — 👍{best.likes} "
                    f"💬{best.comments} 🔁{best.shares}):\n\n{cap}…")
        return "لسه مفيش بوستات منشورة كفاية أحكم منها."

    # Best posting time?
    if has("أحسن وقت", "احسن وقت", "امتى انشر", "إمتى أنشر", "وقت النشر"):
        memory = strategist.ensure_memory(config)
        if memory and memory.best_hours:
            return f"أحسن أوقات نشر ليك (من أداء بوستاتك): {', '.join(memory.best_hours)}."
        return "لسه محتاج أحلّل أداء أكتر عشان أحدّد أحسن وقت — دوس «حلّل صفحتي»."

    return ""


# =====================================================================
# Execute — run the chosen action
# =====================================================================
def execute(config, action: str, params: dict) -> Optional[str]:
    """Dispatch the action to the existing tasks. Returns a short status note
    (Arabic) to append to the reply, or None if nothing was run.

    weekly_report is intentionally NOT dispatched here — the view redirects to the
    report screen for it. analyze_competitor is pure analysis (already in reply).
    """
    params = params or {}
    try:
        if action == "generate_post":
            content_type = _clean_content_type(params.get("content_type"))
            angle = str(params.get("angle") or "").strip()
            occasion = str(params.get("occasion") or "").strip()[:160]
            image_source = _clean_image_source(params.get("image_source"))
            from ..tasks import generate_ideas as gi
            gi.delay(config.id, count=1, image_source=image_source, angle=angle,
                     occasion=occasion, content_type=content_type)
            return "جارٍ توليد البوست… حدّث الصفحة بعد لحظات وهتلاقيه في المسودات."

        if action == "generate_ideas":
            count = _clean_int(params.get("count"), default=10, lo=1, hi=25)
            content_type = _clean_content_type(params.get("content_type"))
            image_source = _clean_image_source(params.get("image_source"))
            from ..tasks import generate_ideas as gi
            gi.delay(config.id, count=count, image_source=image_source,
                     content_type=content_type)
            return f"جارٍ توليد {count} فكرة/مسودة… راجعها في المسودات بعد لحظات."

        if action == "autopost_inventory":
            count = _clean_int(params.get("count"), default=3, lo=1, hi=10)
            strategy = str(params.get("strategy") or "new").strip()
            if strategy not in ("new", "featured", "low"):
                strategy = "new"
            from ..tasks import autopost_from_inventory
            autopost_from_inventory.delay(config.id, count=count, strategy=strategy)
            return f"جارٍ تجهيز {count} بوست منتجات من مخزونك (بالسعر والصورة واللينك)."

        if action == "analyze_page":
            if not config.has_facebook():
                return "لازم تربط صفحة فيسبوك و Page Token من الإعدادات الأول."
            from ..tasks import import_page_posts
            import_page_posts.delay(config.id, 1000)
            return "جارٍ تحليل صفحتك واستيراد بوستاتها والتعلّم من أنجحها… استنى دقيقتين."

        if action == "learn":
            res = strategist.learn(config)
            if res.get("learned"):
                return f"اتعلمت من {res['measured']} بوست وحدّثت الاستراتيجية ✅"
            return "لسه مفيش بيانات أداء كفاية للتعلّم — انشر شوية بوستات الأول."

        if action == "ab_test":
            if not config.has_facebook():
                return "لازم تربط صفحة فيسبوك من الإعدادات الأول."
            angle = str(params.get("angle") or "").strip()
            occasion = str(params.get("occasion") or "").strip()[:160]
            from ..tasks import run_ab_experiment
            run_ab_experiment.delay(config.id, angle=angle, occasion=occasion)
            return "جارٍ تجهيز تجربة A/B بنسختين مختلفتين."

    except Exception:
        logger.warning("social_ads assistant: execute(%s) failed for %s",
                       action, config.tenant.schema_name, exc_info=True)
        return "حصلت مشكلة وانا بنفّذ الطلب — جرّب تاني أو استخدم الأزرار فوق."

    return None


# =====================================================================
# Param cleaners
# =====================================================================
def _clean_content_type(v) -> str:
    v = str(v or "mix").strip().lower()
    return v if v in _CONTENT_TYPES else "mix"


def _clean_image_source(v) -> str:
    v = str(v or "inventory").strip().lower()
    return v if v in ("inventory", "ai", "none") else "inventory"


def _clean_int(v, *, default: int, lo: int, hi: int) -> int:
    try:
        return min(max(int(v), lo), hi)
    except (ValueError, TypeError):
        return default
