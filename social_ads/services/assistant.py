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
        return {
            "reply": (
                "معلش، النظام مشغول شوية دلوقتي. جرّب تاني بعد لحظات، "
                "أو استخدم الأزرار فوق (ولّد أفكار / بوستات من المخزون / حلّل صفحتي)."
            ),
            "action": "none",
            "params": {},
        }

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
