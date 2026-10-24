"""
robot/assistant.py — what makes the robot more than a price-teller.

* Sound in the shop: «شغّل قرآن», «شغّل موسيقى هادية», «وقّف الصوت»,
  «علّي / وطّي الصوت». Only for someone the camera recognized whose role
  allows it (`media`: a manager, a supervisor, the owner) — the ERP decides,
  the robot's brain on the mini PC plays (robot/brain/media.py). A bridge that
  can't play anything (the ESP32) is told so instead of promising it.
* The staff's own questions: «بعنا كام النهارده؟», «إيه اللي قرّب يخلص؟» —
  the dashboard's numbers (ReportingService), so the robot never contradicts
  it, each behind its role.
* Out of stock isn't the end of a sale: a part with the same OEM number on
  the shelf here («بس عندي بديله …»), or the same part in another branch.
"""

from __future__ import annotations

import re
from typing import Optional

from . import permissions
from .wakename import normalize

# ---------------------------------------------------------------------------
# Sound in the shop
# ---------------------------------------------------------------------------

# On normalized text (wakename.normalize: no tashkeel, ة→ه, ى→ي, آ/أ/إ→ا).
# Commands are said as commands: the verb first (after «ممكن» / «لو سمحت» and
# the like), its object right after it. Matched anywhere, «بسأل على كاتم صوت»
# — «على» normalizes to «علي» — was a volume command, and «الزيت قرب يخلص»
# (a customer's oil) was the staff's stock question.
_LEAD = re.compile(r"^((ممكن|لو سمحت|من فضلك|يا ريت|ياريت|please|طب|طيب|بقولك|معلش|بالله)\s+)+")
_VERB_PLAY = r"(?:ت|ن)?(?:شغل|شغلي|شغلنا|شغللنا|افتح|افتحلنا|حط|حطلنا|play)"
_VERB_STOP = r"(?:(?:ت|ن)?(?:وقف|اوقف|اقفل|بطل|اسكت|اطفي|stop)|كفايه)"
_VERB_UP = r"(?:ت|ن)?(?:علي|ارفع|زود|علا|عليلي|علينا)"
_VERB_DOWN = r"(?:ت|ن)?(?:وطي|قلل|خفض|نزل|وطيلي|وطينا)"
_GAP = r"(?:\s+\S+){0,2}?\s+"
_PLAY = re.compile(rf"^{_VERB_PLAY}{_GAP}(?:ال)?(?P<what>قران|قرءان|سوره|ايات|موسيقي|موسيقا|مزيكا|ميوزك|اغاني|quran|music)")
_STOP = re.compile(rf"^{_VERB_STOP}{_GAP}(?:ال)?(?:صوت|قران|موسيقي|موسيقا|مزيكا|تشغيل|اغاني|music|quran)(?:\s|$)")
_LOUDER = re.compile(rf"^{_VERB_UP}{_GAP}(?:ال)?صوت(?:\s|$)")
_QUIETER = re.compile(rf"^{_VERB_DOWN}{_GAP}(?:ال)?صوت(?:\s|$)")
_TODAY = r"(النهارده|انهارده|النهاردا|اليوم|today)"
_SALES = re.compile(rf"^(?:(?:احنا|هو)\s+)?(?:بعنا|بيعنا|اتباع|المبيعات|مبيعات|sold|sales).*{_TODAY}"
                    rf"|^{_TODAY}.*(?:بعنا|بيعنا|اتباع|مبيعات|sold|sales)")
_LOW = re.compile(r"^(?:(?:ايه|ايش|اي|انهي)\s+(?:اللي|الحاجات اللي|الاصناف اللي|الحاجات|الاصناف)?\s*"
                  r"(?:قرب|قربت|قربو|قربوا)\s+(?:ي|ت)?خلص"
                  r"|(?:ايه\s+)?(?:ال)?نواقص|ايه\s+(?:ال)?ناقص|(?:ال)?حاجات\s+(?:ال)?ناقصه|low stock)")


MEDIA_REPLIES = {
    ("play", "quran"): "حاضر، هشغّل القرآن.",
    ("play", "calm"): "حاضر، هشغّل موسيقى هادية.",
    ("stop", ""): "حاضر، وقفت الصوت.",
    ("volume", "up"): "حاضر، علّيت الصوت.",
    ("volume", "down"): "حاضر، وطّيت الصوت.",
}
WHO_ARE_YOU = "معلش، الأمر ده لصاحب الصلاحية بس. بص للكاميرا ثانية علشان أعرفك."
NO_SPEAKERS = "التشغيل محتاج «مخ موس» على الكمبيوتر — البوردة دي مبتشغّلش صوت."


def _said(text: str) -> str:
    return _LEAD.sub("", normalize(text))


def media_command(text: str) -> Optional[dict]:
    """{"do": play|stop|volume, "what": quran|calm|up|down|""} — or None."""
    t = _said(text)
    if _STOP.search(t):
        return {"do": "stop", "what": ""}
    if _LOUDER.search(t):
        return {"do": "volume", "what": "up"}
    if _QUIETER.search(t):
        return {"do": "volume", "what": "down"}
    m = _PLAY.search(t)
    if m:
        quran = m.group("what") in ("قران", "قرءان", "سوره", "ايات", "quran")
        return {"do": "play", "what": "quran" if quran else "calm"}
    return None


def media_reply(command: dict, employee, caps) -> tuple:
    """(reply, payload) for a media command — or a refusal that says why."""
    if "media" not in caps:
        return NO_SPEAKERS, {"action": "media_unavailable"}
    if employee is None:
        return WHO_ARE_YOU, {"action": "denied", "needs": "face"}
    if not permissions.employee_can(employee, "media"):
        return permissions.denial_message("media"), {"action": "denied"}
    reply = MEDIA_REPLIES[(command["do"], command["what"])]
    return reply, {"action": "media", "media": command}


def media_header(payload: dict) -> str:
    """What travels to the brain beside the spoken reply: «play:quran», «stop»."""
    media = (payload or {}).get("media") or {}
    if not media:
        return ""
    return media["do"] + (f":{media['what']}" if media.get("what") else "")


# ---------------------------------------------------------------------------
# The staff's questions
# ---------------------------------------------------------------------------

LOW_STOCK_SAID = 5


def staff_question(text: str) -> Optional[str]:
    """'sales_today' | 'low_stock' | None."""
    t = _said(text)
    if _SALES.search(t):
        return "sales_today"
    if _LOW.search(t):
        return "low_stock"
    return None


def staff_answer(kind: str, device, employee) -> tuple:
    """(reply, payload) — the dashboard's numbers, for the right role only."""
    from inventory.services.reporting_service import ReportingService

    from .services import spoken_price

    action = "sales_report" if kind == "sales_today" else "stock_report"
    if employee is None:
        return WHO_ARE_YOU, {"action": "denied", "needs": "face"}
    if not permissions.employee_can(employee, action):
        return permissions.denial_message(action), {"action": "denied"}
    stats = ReportingService.get_today_dashboard_stats(getattr(employee, "user", None), device.branch)
    if kind == "sales_today":
        count, total = stats["invoices_count"], stats["total_sales_today"] or 0
        if not count:
            reply = "لسه مفيش مبيعات النهارده."
        else:
            reply = f"النهارده اتباع {count} فاتورة بـ {spoken_price(total)}"
            spent = stats["total_expenses_today"] or 0
            reply += f"، والمصروفات {spoken_price(spent)}." if spent else "."
        return reply, {"action": "sales_today", "count": count, "total": float(total)}
    rows = list(stats["low_stock_qs"].order_by("quantity", "product__name")[:LOW_STOCK_SAID])
    total = stats["low_stock_count"]
    if not total:
        return "مفيش حاجة قرّبت تخلص.", {"action": "low_stock", "count": 0}
    listed = "، ".join(f"{inv.product.name} ({inv.quantity})" for inv in rows)
    reply = f"فيه {total} صنف قرّب يخلص: {listed}"
    reply += "، وغيرهم في الداشبورد." if total > len(rows) else "."
    return reply, {"action": "low_stock", "count": total}


# ---------------------------------------------------------------------------
# Out of stock: an equivalent here, or the same part elsewhere
# ---------------------------------------------------------------------------

def part_codes(product) -> set:
    """Every number the part is known by: its own, OEM cross-refs, extras."""
    codes = {str(product.part_number or "")}
    for field in ("oem_cross_reference", "additional_part_numbers"):
        value = getattr(product, field, None) or []
        if isinstance(value, str):
            value = [value]
        if isinstance(value, (list, tuple)):       # JSON fields: anything else isn't a list of codes
            codes |= {str(v) for v in value}
    return {c.strip().lower() for c in codes if c and str(c).strip()}


def alternative_here(product, branch):
    """An in-stock part in this branch sharing a number with `product`
    (most on hand first, then by part number — the offline brain picks the
    same one), or None."""
    found = alternatives_here(product, branch, limit=1)
    return found[0] if found else None


def alternatives_here(product, branch, limit: int = 6) -> list:
    """The in-stock parts in this branch sharing a number with `product`,
    most on hand first, then by part number."""
    from django.db.models import Q

    from inventory.models import Inventory

    codes = part_codes(product)
    if not codes:
        return []
    # 🐛 [FIX]: the codes are lowercased: `part_number__in` is case-sensitive on
    # Postgres and missed «LR012345». And the JSON prefilter only runs for
    # codes long enough to mean something — a short one matched half the
    # catalog's JSON text and, capped at 50 rows, pushed the real match out.
    q = Q()
    for code in codes:
        q |= Q(product__part_number__iexact=code)
        if len(code) >= 5:
            q |= Q(product__oem_cross_reference__icontains=code)
            q |= Q(product__additional_part_numbers__icontains=code)
    rows = (Inventory.objects.filter(q, branch=branch, quantity__gt=0, product__is_active=True)
            .exclude(product=product).select_related("product")
            .order_by("-quantity", "product__part_number")[:500])
    found = []
    for inv in rows:
        if part_codes(inv.product) & codes:      # icontains is a prefilter, this is the match
            found.append(inv.product)
            if len(found) >= limit:
                break
    return found


def other_branches(product, branch, limit: int = 2) -> list:
    from inventory.models import Inventory

    return list(Inventory.objects.filter(product=product, quantity__gt=0)
                .exclude(branch=branch).order_by("-quantity", "branch__name")
                .values_list("branch__name", flat=True)[:limit])


def branch_counts(product, branch, limit: int = 4) -> list:
    """[(branch name, quantity)] of the other branches that have `product`,
    most first — «في أنهي فرع؟»."""
    from inventory.models import Inventory

    rows = (Inventory.objects.filter(product=product, quantity__gt=0)
            .exclude(branch=branch).order_by("-quantity", "branch__name")
            .values_list("branch__name", "quantity")[:limit])
    return [(name, int(qty)) for name, qty in rows]


def out_of_stock_tail(product, branch) -> tuple:
    """(what to add after «… مش موجود هنا دلوقتي», payload)."""
    from .brain.knowledge import AVAILABLE
    from .services import spoken_price

    alt = alternative_here(product, branch)
    if alt is not None:
        price = alt.retail_price or 0
        tail = f" بس عندي بديله {alt.name} {AVAILABLE}" + (f" بسعر {spoken_price(price)}." if price > 0 else ".")
        return tail, {"alternative": {"id": alt.id, "name": alt.name, "part_number": alt.part_number,
                                      "retail_price": float(price)}}
    names = other_branches(product, branch)
    if names:
        return f" بس موجود في فرع {' وفرع '.join(names)}.", {"other_branches": names}
    return "", {}


# ---------------------------------------------------------------------------
# The morning: what a manager hears when the camera clocks them in
# ---------------------------------------------------------------------------

def _yesterday_sales(branch) -> tuple:
    """(invoices, net sales) of yesterday in `branch` — the dashboard's own
    rules (ReportingService): no quotations, returns netted out."""
    from datetime import timedelta

    from django.utils import timezone

    from inventory.models import SaleInvoice
    from inventory.services.reporting_service import ReportingService

    today_start = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)
    invoices = (SaleInvoice.objects
                .filter(date_created__gte=today_start - timedelta(days=1), date_created__lt=today_start)
                .exclude(status="quotation"))
    if branch is not None:
        invoices = invoices.filter(branch=branch)
    net, _profit = ReportingService.net_sales_profit(invoices)
    return invoices.filter(is_return=False).count(), net or 0


def morning_briefing(employee, device) -> str:
    """Yesterday in one breath, then what's waiting today — each line only
    for a role allowed to hear it (permissions: `sales_report`,
    `stock_report`, `teach`). '' when there's nothing for this person."""
    from datetime import timedelta

    from django.utils import timezone

    from .models import ProcurementSignal, RobotVoiceInteraction
    from .services import spoken_price

    branch = device.branch
    said = []
    if permissions.employee_can(employee, "sales_report"):
        count, total = _yesterday_sales(branch)
        said.append(f"إمبارح اتباع {count} فاتورة بـ {spoken_price(total)}." if count
                    else "إمبارح مكانش فيه مبيعات.")
    if permissions.employee_can(employee, "stock_report"):
        from inventory.services.reporting_service import ReportingService
        low = ReportingService.get_today_dashboard_stats(getattr(employee, "user", None), branch)["low_stock_count"]
        wanted = ProcurementSignal.objects.filter(branch=branch, status="open").count()
        if low:
            said.append(f"فيه {low} صنف قرّب يخلص.")
        if wanted:
            said.append(f"و{wanted} صنف العملاء طلبوه وهو خلصان.")
    if permissions.employee_can(employee, "sales_report") or permissions.employee_can(employee, "stock_report"):
        from . import restock
        back = restock.arrived_count(branch)
        if back:
            said.append(f"و{back} عميل مستنيين أصناف وصلت، ابعتلهم من صفحة «مستنيين أصناف».")
    if permissions.employee_can(employee, "teach"):
        since = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
        unanswered = RobotVoiceInteraction.objects.filter(
            device__branch=branch, created_at__gte=since, payload__unresolved=True).count()
        if unanswered:
            said.append(f"وفيه {unanswered} سؤال معرفتش أرد عليه، علّمهولي من صفحة الروبوت.")
    return " ".join(said)


# ---------------------------------------------------------------------------
# What the ERP doesn't know: a manager asks, the robot looks it up
# ---------------------------------------------------------------------------

WEB_DOWN = "معلش، مقدرتش أدوّر دلوقتي. جرّب كمان شوية."


def web_question(text: str) -> bool:
    """«دوّر على النت على …», «اسأل جوجل …» (robot/brain/talk.py)."""
    from .brain import talk
    return talk.web_question(text)


def may_look_up(employee, text: str) -> bool:
    """A question nothing in the ERP answered, from someone allowed to have
    it looked up — and not one about the stock («عندك …؟» stays «ملقتش»)."""
    from .brain import talk
    return permissions.employee_can(employee, "web_search") and not talk.about_the_stock(text)


def web_reply(text: str, employee, device, lang: str = "ar") -> tuple:
    """(reply, payload): the internet's answer and where it's from — for a
    role allowed to ask (`web_search`)."""
    from . import memory, services

    if employee is None:
        return WHO_ARE_YOU, {"action": "denied", "needs": "face"}
    if not permissions.employee_can(employee, "web_search"):
        return permissions.denial_message("web_search"), {"action": "denied"}
    facts, turns = memory.thinking_context(device)
    answer, sources = services.web_answer(text, lang, facts=facts, history=turns)
    if not answer:
        return WEB_DOWN, {"action": "web_unavailable"}
    site = next((s["title"] for s in sources if s.get("title")), "")
    if site:
        answer += (f" (source: {site[:40]})" if lang == "en" else f" (المصدر: {site[:40]})")
    return answer, {"action": "web", "web": True, "sources": sources[:5]}
