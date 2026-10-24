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
_PLAY = re.compile(r"(^|\s)(شغل|شغلي|شغلنا|شغللنا|افتح|حط|حطلنا|ابدا|play)(\s|$)")
_QURAN = re.compile(r"(قران|قرءان|سوره|سور|ايات|quran)")
_MUSIC = re.compile(r"(موسيقي|موسيقا|مزيكا|ميوزك|music)")
_STOP = re.compile(r"(^|\s)(وقف|اوقف|اقفل|بطل|اسكت|اطفي|كفايه|stop)(\s|$)")
_SOUND = re.compile(r"(صوت|قران|موسيقي|موسيقا|مزيكا|تشغيل|اغاني|music|quran)")
_LOUDER = re.compile(r"(^|\s)(علي|ارفع|زود|علا)(\s|$).*صوت")
_QUIETER = re.compile(r"(^|\s)(وطي|قلل|خفض|نزل|واطي)(\s|$).*صوت")

MEDIA_REPLIES = {
    ("play", "quran"): "حاضر، هشغّل القرآن.",
    ("play", "calm"): "حاضر، هشغّل موسيقى هادية.",
    ("stop", ""): "حاضر، وقفت الصوت.",
    ("volume", "up"): "حاضر، علّيت الصوت.",
    ("volume", "down"): "حاضر، وطّيت الصوت.",
}
WHO_ARE_YOU = "معلش، الأمر ده لصاحب الصلاحية بس. بص للكاميرا ثانية علشان أعرفك."
NO_SPEAKERS = "التشغيل محتاج «مخ موس» على الكمبيوتر — البوردة دي مبتشغّلش صوت."


def media_command(text: str) -> Optional[dict]:
    """{"do": play|stop|volume, "what": quran|calm|up|down|""} — or None."""
    t = normalize(text)
    if _STOP.search(t) and _SOUND.search(t):
        return {"do": "stop", "what": ""}
    if _LOUDER.search(t):
        return {"do": "volume", "what": "up"}
    if _QUIETER.search(t):
        return {"do": "volume", "what": "down"}
    if _PLAY.search(t):
        if _QURAN.search(t):
            return {"do": "play", "what": "quran"}
        if _MUSIC.search(t):
            return {"do": "play", "what": "calm"}
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

_TODAY = r"(النهارده|انهارده|النهاردا|اليوم|today)"
_SALES = re.compile(rf"(بعنا|بيعنا|اتباع|مبيعات|المبيعات|sold|sales).*{_TODAY}|{_TODAY}.*(بعنا|بيعنا|اتباع|مبيعات|sold|sales)")
_LOW = re.compile(r"(قرب|قربت|قربو|قربوا)\s+(ي|ت)?خلص|النواقص|نواقص|الناقص|الحاجات الناقصه|low stock")
LOW_STOCK_SAID = 5


def staff_question(text: str) -> Optional[str]:
    """'sales_today' | 'low_stock' | None."""
    t = normalize(text)
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
    from django.db.models import Q

    from inventory.models import Inventory

    codes = part_codes(product)
    if not codes:
        return None
    q = Q(product__part_number__in=list(codes))
    for code in codes:
        q |= Q(product__oem_cross_reference__icontains=code)
        q |= Q(product__additional_part_numbers__icontains=code)
    rows = (Inventory.objects.filter(q, branch=branch, quantity__gt=0, product__is_active=True)
            .exclude(product=product).select_related("product")
            .order_by("-quantity", "product__part_number")[:50])
    for inv in rows:
        if part_codes(inv.product) & codes:      # icontains is a prefilter, this is the match
            return inv.product
    return None


def other_branches(product, branch, limit: int = 2) -> list:
    from inventory.models import Inventory

    return list(Inventory.objects.filter(product=product, quantity__gt=0)
                .exclude(branch=branch).order_by("-quantity", "branch__name")
                .values_list("branch__name", flat=True)[:limit])


def out_of_stock_tail(product, branch) -> tuple:
    """(what to add after «… مش متوفر حالياً في الفرع», payload)."""
    from .services import spoken_price

    alt = alternative_here(product, branch)
    if alt is not None:
        price = alt.retail_price or 0
        tail = f" بس عندي بديله {alt.name} متوفر" + (f" بسعر {spoken_price(price)}." if price > 0 else ".")
        return tail, {"alternative": {"id": alt.id, "name": alt.name, "part_number": alt.part_number,
                                      "retail_price": float(price)}}
    names = other_branches(product, branch)
    if names:
        return f" بس موجود في فرع {' وفرع '.join(names)}.", {"other_branches": names}
    return "", {}
