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
ABSENT_SAID = 6

_HOW_MUCH = r"(?:كام|قد ايه|اد ايه|قد اي)"
_SAFE = r"(?:ال)?(?:خزنه|خزينه|خزن|درج)"
_TREASURY = re.compile(rf"(?:رصيد|فلوس)\s+{_SAFE}|{_SAFE}\s+(?:فيها|فيه|بها)\s+{_HOW_MUCH}"
                       rf"|(?:في|ف|جوه)\s+{_SAFE}\s+{_HOW_MUCH}|{_HOW_MUCH}\s+(?:في|ف|جوه)\s+{_SAFE}"
                       rf"|معانا\s+{_HOW_MUCH}\s+(?:في|ف)\s+{_SAFE}")
_SPENT = re.compile(rf"^(?:(?:احنا)\s+)?(?:صرفنا|(?:ال)?مصاريف|(?:ال)?مصروفات)\s+(?:{_HOW_MUCH}\s+)?{_TODAY}(?:\s+{_HOW_MUCH})?$"
                    rf"|^{_TODAY}\s+(?:صرفنا|(?:ال)?مصاريف|(?:ال)?مصروفات)\s+{_HOW_MUCH}$")
_MONTH = r"(?:الشهر ده|الشهر دا|الشهر|هذا الشهر|this month)"
_YESTERDAY = r"(?:امبارح|إمبارح|البارحه|yesterday)"
_SOLD = r"(?:بعنا|بيعنا|اتباع|(?:ال)?مبيعات)"
_SALES_MONTH = re.compile(rf"^(?:(?:احنا|هو)\s+)?{_SOLD}.*{_MONTH}|^{_MONTH}.*{_SOLD}")
_SALES_YESTERDAY = re.compile(rf"^(?:(?:احنا|هو)\s+)?{_SOLD}.*{_YESTERDAY}|^{_YESTERDAY}.*{_SOLD}")
_PRESENT = re.compile(r"^(?:مين|من)\s+(?:اللي\s+)?(?:حاضر|حضر|جه|جاي|موجود)(?:\s|$)|^(?:ال)?حضور\s|^(?:ال)?حضور$")
_ABSENT = re.compile(r"^(?:مين|من)\s+(?:اللي\s+)?(?:غايب|غاب|مجاش|ماجاش|مش موجود|مش جاي)(?:\s|$)|^(?:ال)?غياب(?:\s|$)")
_CUSTOMER_OWES = re.compile(rf"^(?:كشف\s+)?حساب\s+(?:ال)?عميل\s+(?P<a>.+?)(?:\s+{_HOW_MUCH})?$"
                            rf"|^(?:ال)?عميل\s+(?P<b>.+?)\s+(?:عليه|عليها|حسابه)\s+{_HOW_MUCH}$"
                            rf"|^{_HOW_MUCH}\s+علي\s+(?:ال)?عميل\s+(?P<c>.+)$")
_SOMEONE_OWES = re.compile(rf"^(?P<a>.+?)\s+(?:عليه|عليها)\s+{_HOW_MUCH}$|^{_HOW_MUCH}\s+علي\s+(?P<b>.+)$")
_VENDOR_OWED = re.compile(rf"^(?:كشف\s+)?حساب\s+(?:ال)?مورد\s+(?P<a>.+?)(?:\s+{_HOW_MUCH})?$"
                          rf"|^(?:ال)?مورد\s+(?P<b>.+?)\s+(?:ليه|له|ليها|لها|حسابه)\s+{_HOW_MUCH}(?:\s+عندنا)?$"
                          rf"|^(?:علينا|عليا)\s+{_HOW_MUCH}\s+(?:لل?مورد|ل)\s*(?P<c>.+)$"
                          rf"|^{_HOW_MUCH}\s+(?:علينا|عليا)\s+(?:لل?مورد|ل)\s*(?P<d>.+)$")
_DAY = re.compile(r"^(?:قفل|قفلي|اقفل|تقفيل)\s+(?:ال)?(?:يوم|يوميه|ورديه)(?:\s|$)"
                  rf"|^(?:ملخص|حساب|تقرير)\s+(?:ال)?(?:يوم|يوميه|ورديه|{_TODAY})(?:\s|$)")
# «سجل حضوري», «سجل انصرافي» — one's own, whoever they are. Said as a command only: «ماشي» and
# «أنا جيت» are what customers say too.
_CLOCK_IN = re.compile(r"^(?:سجل|سجلي|اعمل|اعملي)\s+(?:لي\s+)?(?:حضوري|حضور|الحضور)(?:\s|$)")
_CLOCK_OUT = re.compile(r"^(?:سجل|سجلي|اعمل|اعملي)\s+(?:لي\s+)?(?:انصرافي|انصراف|الانصراف|خروجي)(?:\s|$)")
HEADS_UP_AFTER = frozenset({"sales_today", "day_summary", "treasury"})   # the manager's own look at the day
OVERDUE_DAYS = 30
# What each question needs (robot/permissions.py).
_NEEDS = {"day_summary": "sales_report", "sales_today": "sales_report", "sales_month": "sales_report", "sales_yesterday": "sales_report",
          "spent_today": "sales_report", "low_stock": "stock_report", "treasury": "treasury_report",
          "customer_balance": "accounts_report", "vendor_balance": "accounts_report",
          "present": "attendance_report", "absent": "attendance_report"}


def _named(match) -> str:
    return next((g.strip() for g in match.groups() if g and g.strip()), "") if match else ""


def staff_question(text: str) -> Optional[str]:
    """Which of the staff's questions this is — a key of `_NEEDS` — or None."""
    t = _said(text)
    if _SALES.search(t):
        return "sales_today"
    if _LOW.search(t):
        return "low_stock"
    if any(ch.isdigit() for ch in t):            # «مصاريف 200 بنزين النهارده» is an entry, not a question
        return None
    if _DAY.search(t):
        return "day_summary"
    if _SALES_MONTH.search(t):
        return "sales_month"
    if _SALES_YESTERDAY.search(t):
        return "sales_yesterday"
    if _SPENT.search(t):
        return "spent_today"
    if _TREASURY.search(t):
        return "treasury"
    if _VENDOR_OWED.search(t):
        return "vendor_balance"
    if _CUSTOMER_OWES.search(t):
        return "customer_balance"
    if _ABSENT.search(t):
        return "absent"
    if _PRESENT.search(t):
        return "present"
    # «أحمد محمود عليه كام؟» — only of someone registered: «الفلتر عليه كام» is about a part.
    bare = _SOMEONE_OWES.search(t)
    if bare:
        from . import entries
        who, count = entries.find_customer(_named(bare))
        if who is not None or count:
            return "customer_balance"
    return None


def staff_answer(kind: str, device, employee, text: str = "") -> tuple:
    """(reply, payload) — the dashboard's numbers, for the right role only."""
    from inventory.services.reporting_service import ReportingService

    from .services import spoken_price

    action = _NEEDS.get(kind, "sales_report")
    if employee is None:
        return WHO_ARE_YOU, {"action": "denied", "needs": "face"}
    if not permissions.employee_can(employee, action):
        return permissions.denial_message(action), {"action": "denied"}
    if kind == "day_summary":
        return _day_summary(device, employee)
    if kind in ("sales_month", "sales_yesterday"):
        return _sales_of(kind, device)
    if kind == "treasury":
        return _treasury_answer(device, employee)
    if kind in ("customer_balance", "vendor_balance"):
        return _balance_answer(kind, _said(text))
    if kind in ("present", "absent"):
        return _attendance_answer(kind)
    stats = ReportingService.get_today_dashboard_stats(getattr(employee, "user", None), device.branch)
    if kind == "spent_today":
        spent = stats["total_expenses_today"] or 0
        reply = f"مصاريف النهارده {spoken_price(spent)}." if spent else "لسه مفيش مصاريف النهارده."
        return reply, {"action": "spent_today", "total": float(spent)}
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


def _day_summary(device, employee) -> tuple:
    """«قفّل اليوم»: what was sold, spent and collected today on the robot's branch, and what the
    treasuries hold now — the dashboard's own numbers."""
    from django.db.models import Sum
    from django.utils import timezone
    from inventory.models import FinancialTransaction
    from inventory.services.reporting_service import ReportingService

    from .services import spoken_price

    user = getattr(employee, "user", None)
    stats = ReportingService.get_today_dashboard_stats(user, device.branch)
    start = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)
    collected = FinancialTransaction.objects.filter(transaction_type="in", date__gte=start, customer__isnull=False,
                                                    sale_invoice__is_return=False)
    if device.branch is not None:
        collected = collected.filter(treasury__branch=device.branch)
    collected = collected.aggregate(s=Sum("amount"))["s"] or 0
    safe = ReportingService.get_treasury_summary(user, device.branch)["total_treasury_balance"]
    count, sold, spent = stats["invoices_count"], stats["total_sales_today"] or 0, stats["total_expenses_today"] or 0
    parts = [f"اتباع {count} فاتورة بـ {spoken_price(sold)}" if count else "مفيش مبيعات",
             f"المصاريف {spoken_price(spent)}" if spent else "مفيش مصاريف"]
    if collected:
        parts.append(f"اتحصل من العملاء {spoken_price(collected)}")
    parts.append(f"والخزنة فيها دلوقتي {spoken_price(safe)}")
    return ("ملخص النهارده: " + "، ".join(parts) + ".",
            {"action": "day_summary", "count": count, "sold": float(sold), "spent": float(spent),
             "collected": float(collected), "safe": float(safe)})


def heads_up(device, employee) -> str:
    """Once a day, to whoever looks at the day's numbers: what needs them — parts running low,
    customers owing for over a month. '' when nothing does, or they were told today."""
    from django.core.cache import cache
    from django.utils import timezone

    if employee is None or getattr(employee, "pk", None) is None:
        return ""
    key = f"robot:headsup:{device.pk}:{employee.pk}:{timezone.localdate().isoformat()}"
    try:
        if not cache.add(key, 1, 20 * 3600):
            return ""
    except Exception:
        return ""
    said = []
    if permissions.employee_can(employee, "stock_report"):
        from inventory.services.reporting_service import ReportingService
        low = ReportingService.get_today_dashboard_stats(getattr(employee, "user", None), device.branch)["low_stock_count"]
        if low:
            said.append(f"فيه {low} صنف قرّب يخلص")
    if permissions.employee_can(employee, "accounts_report"):
        late = _overdue_customers(device.branch)
        if late:
            said.append(f"فيه {late} عميل عليهم فلوس بقالها أكتر من شهر")
    return ("على فكرة، " + "، و".join(said) + ".") if said else ""


def _overdue_customers(branch) -> int:
    """Customers with a posted sale left unpaid for over OVERDUE_DAYS."""
    from datetime import timedelta

    from django.db.models import F
    from django.utils import timezone
    from inventory.models import SaleInvoice

    old = (SaleInvoice.objects.filter(status="posted", is_return=False, customer__balance__gt=0,
                                      date_created__lt=timezone.now() - timedelta(days=OVERDUE_DAYS))
           .exclude(paid_amount__gte=F("total_amount")))
    if branch is not None:
        old = old.filter(branch=branch)
    return old.values("customer_id").distinct().count()


def attendance_command(text: str) -> Optional[str]:
    """'in' | 'out' | None."""
    t = _said(text)
    if _CLOCK_OUT.search(t):
        return "out"
    if _CLOCK_IN.search(t):
        return "in"
    return None


def attendance_reply(do: str, employee) -> tuple:
    """One's own clock-in / clock-out, by HR's own service (lateness, hours, its rules)."""
    from django.core.exceptions import ValidationError
    from django.utils import timezone
    from hr.services.attendance_service import AttendanceService

    if employee is None:
        return WHO_ARE_YOU, {"action": "denied", "needs": "face"}
    name = (employee.user.first_name or employee.user.get_full_name() or employee.user.username).strip()
    try:
        if do == "in":
            record = AttendanceService.clock_in(employee, face_verified=bool(getattr(employee, "robot_saw_face", False)))
            at = timezone.localtime(record.clock_in)
            late = int(getattr(record, "late_minutes", 0) or 0)
            reply = f"صباح الفل يا {name}، سجلت حضورك الساعة {at:%H:%M}."
            if late > 0:
                reply += f" متأخر {late} دقيقة."
        else:
            record = AttendanceService.clock_out(employee)
            at = timezone.localtime(record.clock_out)
            reply = f"مع السلامة يا {name}، سجلت انصرافك الساعة {at:%H:%M}."
    except ValidationError as exc:
        return " ".join(str(m) for m in exc.messages), {"action": "attendance_refused", "do": do}
    return reply, {"action": f"clock_{do}", "employee_id": employee.pk}


def _sales_of(kind: str, device) -> tuple:
    """This month's sales, or yesterday's, on the robot's branch — net of returns, as the dashboard counts."""
    from datetime import timedelta

    from django.utils import timezone
    from inventory.models import SaleInvoice
    from inventory.services.reporting_service import ReportingService

    from .services import spoken_price

    now = timezone.localtime()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if kind == "sales_month":
        start, end, when = today.replace(day=1), None, "الشهر ده"
    else:
        start, end, when = today - timedelta(days=1), today, "امبارح"
    qs = SaleInvoice.objects.filter(date_created__gte=start).exclude(status="quotation")
    if end is not None:
        qs = qs.filter(date_created__lt=end)
    if device.branch is not None:
        qs = qs.filter(branch=device.branch)
    total, _profit = ReportingService.net_sales_profit(qs)
    count = qs.filter(is_return=False).count()
    reply = (f"{when} اتباع {count} فاتورة بـ {spoken_price(total)}." if count
             else f"مفيش مبيعات {when}.")
    return reply, {"action": kind, "count": count, "total": float(total)}


def _treasury_answer(device, employee) -> tuple:
    from inventory.services.reporting_service import ReportingService

    from .services import spoken_price

    summary = ReportingService.get_treasury_summary(getattr(employee, "user", None), device.branch)
    rows = summary["treasuries_data"]
    if not rows:
        return "الفرع ده مالوش خزنة شغالة.", {"action": "treasury", "count": 0}
    if len(rows) == 1:
        reply = f"{rows[0]['name']} فيها {spoken_price(rows[0]['balance'])}."
    else:
        listed = "، ".join(f"{r['name']} فيها {spoken_price(r['balance'])}" for r in rows[:4])
        reply = f"{listed}. الإجمالي {spoken_price(summary['total_treasury_balance'])}."
    return reply, {"action": "treasury", "count": len(rows), "total": float(summary["total_treasury_balance"])}


def _balance_answer(kind: str, said: str) -> tuple:
    from . import entries
    from .services import spoken_price

    if kind == "vendor_balance":
        name = _named(_VENDOR_OWED.search(said))
        vendor = entries.find_vendor(name) if name else None
        if vendor is None:
            return f"مش لاقي المورد «{name}» في الموردين.", {"action": kind, "found": False}
        owed = vendor.balance or 0
        if owed > 0:
            reply = f"علينا للمورد {vendor.name} {spoken_price(owed)}."
        elif owed < 0:
            reply = f"المورد {vendor.name} ليه عندنا مقدم {spoken_price(-owed)}."
        else:
            reply = f"مفيش حاجة علينا للمورد {vendor.name}."
        return reply, {"action": kind, "found": True, "balance": float(owed)}
    name = _named(_CUSTOMER_OWES.search(said)) or _named(_SOMEONE_OWES.search(said))
    customer, count = entries.find_customer(name) if name else (None, 0)
    if customer is None:
        if count > 1:
            return f"فيه أكتر من عميل اسمه {name}. قول اسمه كامل زي ما هو متسجل.", {"action": kind, "found": False}
        return f"مش لاقي عميل متسجل اسمه «{name}».", {"action": kind, "found": False}
    owed = customer.balance or 0
    if owed > 0:
        reply = f"{customer.name} عليه {spoken_price(owed)}."
    elif owed < 0:
        reply = f"{customer.name} ليه عندنا {spoken_price(-owed)}."
    else:
        reply = f"{customer.name} مش عليه حاجة."
    return reply, {"action": kind, "found": True, "balance": float(owed)}


def _attendance_answer(kind: str) -> tuple:
    from django.utils import timezone
    from hr.models import AttendanceRecord, Employee

    today = timezone.localdate()
    records = (AttendanceRecord.objects.filter(date=today).select_related("employee__user"))
    here = [r.employee for r in records if r.status in ("present", "late") and r.clock_in is not None]
    here_ids = {e.pk for e in here}

    def called(emp):
        return emp.user.get_full_name() or emp.user.username

    if kind == "present":
        if not here:
            return "لسه محدش سجل حضور النهارده.", {"action": kind, "count": 0}
        names = "، ".join(called(e) for e in here[:ABSENT_SAID])
        more = f"، و{len(here) - ABSENT_SAID} غيرهم" if len(here) > ABSENT_SAID else ""
        return f"حاضر النهارده {len(here)}: {names}{more}.", {"action": kind, "count": len(here)}
    excused = {r.employee_id for r in records if r.status in ("excused", "holiday")}
    away = [e for e in Employee.objects.select_related("user").filter(is_active=True, user__is_active=True)
            if e.pk not in here_ids and e.pk not in excused]
    if not away:
        return "مفيش حد غايب النهارده.", {"action": kind, "count": 0}
    names = "، ".join(called(e) for e in away[:ABSENT_SAID])
    more = f"، و{len(away) - ABSENT_SAID} غيرهم" if len(away) > ABSENT_SAID else ""
    return f"لسه مسجلش حضور النهارده {len(away)}: {names}{more}.", {"action": kind, "count": len(away)}


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
    from .brain import talk
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
        said = " وفرع ".join(talk.branch_said(n) for n in names)
        return f" بس موجود في فرع {said}.", {"other_branches": names}
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
