"""
robot/money.py — money the robot takes in and pays out by voice, besides invoices and expenses.

    «يا موس، حصّل 500 جنيه من أحمد محمود»     → تحصيل من عميل آجل (inventory.services.customer_payments)
    «يا موس، ادفع 1000 للمورد الأمل»          → دفعة لمورد (inventory.services.vendor_payments)
    «يا موس، سجل سلفة 500 للموظف محمد علي»    → سلفة موظف (hr.services.advance_service)

    «يا موس، رجّع فاتورة رقم 125» / «رجّع 1 فلتر زيت من فاتورة 125» → مرتجع بيع (InvoiceService)
    «يا موس، حوّل 500 من خزنة الكاش لخزنة البنك»   → تحويل بين خزن الفرع
    «يا موس، احسب مرتبات الشهر ده» / «اصرف مرتبات شهر 10» → المرتبات (hr PayrollService)

The first three are held like an expense (robot/entries.py): the robot says what it understood and asks
«أسجلها على أنهي فرع؟»; the money moves through that branch's cash treasury, by the ERP's own
services — the same rules as its screens:

  • A collection: a customer registered by name, never «عميل نقدي» (shared by everyone: collected
    from the invoice itself), and never more than what they owe.
  • A vendor payment: never more than what the shop owes them (a number misheard would be an
    advance nobody meant) — advances stay on the vendors' screen.
  • An advance: HR's policy (the most of a salary, one at a time). Whoever may approve advances
    (an HR manager, the company's admin) has it approved and paid at once; anyone else's is a
    request, waiting for them.

A return, a transfer and paying the salaries belong to one place already (the invoice's branch,
the robot's own, the company): they're confirmed with «أيوه» instead of a branch.

  • A return: a posted sale invoice by its number — all that's left of it, or the pieces named;
    the refund (what was really paid, after what's still owed) leaves the invoice's branch till,
    refused when it can't cover it. The ERP's own return: stock back, the customer's account.
  • A transfer: between two active treasuries of the robot's branch, never more than the source has.
  • The salaries: «احسب» only calculates the month (no money, can be done again); «اصرف» approves
    and pays a calculated month — a manager's, and refused whole when no treasury covers a salary.

Who may: robot/permissions (`collect`, `vendor_pay`, `advance`, `return`, `transfer`, `payroll`,
`payroll_pay`) — the face the camera knows, or a
PIN (robot/wizard.py).
"""

from __future__ import annotations

import re
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction

from . import wakename

_NUMBER = r"(\d+(?:[.,]\d+)?)"
_MONEY = r"\s*(?:جنيهات|جنيها|جنيه|جنية|ج\.?م|egp)?(?!\w)"
_LEAD = r"^(?:(?:سجل|سجلي|اعمل|اعملي|اكتب|ضيف|اضف)\s+)?"
# On normalized words (wakename.normalize: ة→ه، أ→ا، no tashkeel).
_COLLECT = re.compile(_LEAD + r"(?:تحصيل|حصل|حصلي|حصلت|حصلنا|استلمت|استلمنا|استلم|قبضت|قبضنا)\s+(.+)$")
_PAID_US = re.compile(r"^العميل\s+(.+?)\s+(?:دفع|دفعلي|دفعلنا|سدد|سددلنا)\s+(.+)$")
_VENDOR = re.compile(_LEAD + r"(?:ادفع|ادفعلي|ندفع|سدد|سددنا|سددت|دفعنا|دفعت|دفعه|سداد)\s+(.+)$")
_VENDOR_WORD = re.compile(r"(?:^|\s)(?:لل?مورد|مورد|المورد)(?:\s|$)")
_ADVANCE = re.compile(_LEAD + r"(?:(?:اصرف|ادي|اديله|ادي له)\s+)?(?:سلفه|سلفيه)\s+(.+)$")
_NAME_LEAD = re.compile(r"^(?:من|ل|لل|الي|علي|عند|باسم|العميل|للعميل|المورد|للمورد|لمورد|مورد|الموظف|للموظف|لموظف|"
                        r"الاستاذ|للاستاذ|استاذ|الاسطي|للاسطي|يا)\s+")
_NAME_TAIL = re.compile(r"\s+(?:كاش|نقدي|نقدا|من حسابه|من الحساب|علي الحساب|تحت الحساب|لو سمحت)$")

_RETURN = re.compile(_LEAD + r"(?:مرتجع|رجع|رجعلي|ارجع|استرجع|استرجاع)(?:\s+(.+))?$")
_INVOICE_NO = re.compile(r"(?:ال)?فاتوره\s+(?:رقم\s+|نمره\s+)?(\d+)")
_TRANSFER = re.compile(_LEAD + r"(?:حول|حولي|انقل|تحويل)\s+(.+)$")
_FROM_TO = re.compile(r"من\s+(?P<src>.+?)\s+(?:الي\s+|علي\s+|لل|ل(?=خز|در|حساب|ال|بنك|فيزا|محفظ|كاش))(?P<dst>.+)$")
_PAYROLL = re.compile(r"^(?P<do>احسب|احسبلي|جهز|جهزلي|طلع|طلعلي|اعمل|اصرف|ادفع|نزل|صرف)\s+(?:كشف\s+)?"
                      r"(?:ال)?(?:مرتبات|رواتب|مرتب الموظفين)(?P<rest>.*)$")
_PAY_VERBS = frozenset({"اصرف", "ادفع", "نزل", "صرف"})
_SAFE_WORDS = re.compile(r"^(?:ال)?(?:خزنه|خزينه|درج|حساب)\s+")
_SAFE_TYPES = {"كاش": "cash", "الكاش": "cash", "نقدي": "cash", "النقديه": "cash", "بنك": "bank", "البنك": "bank",
               "البنكي": "bank", "فيزا": "visa", "الفيزا": "visa", "محفظه": "wallet", "المحفظه": "wallet"}

KINDS = ("collection", "vendor_pay", "advance", "return", "transfer", "payroll")
CONFIRMED = ("return", "transfer", "payroll")        # «أيوه» — their place is known, no branch asked
EXAMPLES = {
    "collection": "«حصّل 500 جنيه من أحمد محمود»",
    "vendor_pay": "«ادفع 1000 جنيه للمورد الأمل»",
    "advance": "«سجل سلفة 500 جنيه للموظف محمد علي»",
    "return": "«رجّع فاتورة رقم 125» أو «رجّع 1 فلتر زيت من فاتورة رقم 125»",
    "transfer": "«حوّل 500 جنيه من خزنة الكاش لخزنة البنك»",
}


def _said(text: str) -> str:
    from .entries import _said as digits        # amounts said in words: «خمسمية» → 500
    return wakename.normalize(digits(text))


def _amount_and_name(rest: str):
    """(amount or None, the name left) from «500 جنيه من احمد محمود» / «لمحمد علي 500»."""
    from .entries import _to_decimal

    num = re.search(_NUMBER + _MONEY, rest)
    amount = _to_decimal(num.group(1)) if num else None
    name = (rest[:num.start()] + " " + rest[num.end():]) if num else rest
    name = " ".join(name.split())
    for _ in range(3):                          # «من العميل الاستاذ احمد»
        name = _NAME_LEAD.sub("", name)
    name = _NAME_TAIL.sub("", name).strip()
    return amount, name


def parse(text: str):
    """{"kind", "amount", "name"} for a collection, a vendor payment or an advance — else None.
    `amount` None / `name` '' when it's clearly one and that wasn't said (asked for)."""
    t = _said(text)
    m = _PAYROLL.match(t)
    if m:
        month, year = _month_said(m.group("rest"))
        return {"kind": "payroll", "pay": m.group("do") in _PAY_VERBS, "month": month, "year": year,
                "amount": None, "name": ""}
    m = _RETURN.match(t)
    if m and (_INVOICE_NO.search(t) or "فاتوره" in t.split() or "الفاتوره" in t.split()
              or re.match(_LEAD + r"مرتجع(?:\s|$)", t)):       # «رجع الصوت» isn't one
        number = _INVOICE_NO.search(t)
        what = _INVOICE_NO.sub(" ", m.group(1) or "")
        what = " ".join(w for w in what.split() if w not in ("من", "في", "بتاع", "بتاعه", "كلها", "كله", "كامله", "بيع"))
        return {"kind": "return", "invoice": int(number.group(1)) if number else None, "what": what,
                "amount": None, "name": ""}
    m = _TRANSFER.match(t)
    if m:
        from .entries import _to_decimal
        num = re.search(_NUMBER + _MONEY, m.group(1))
        ends = _FROM_TO.search(m.group(1)[num.end():] if num else m.group(1))
        return {"kind": "transfer", "amount": _to_decimal(num.group(1)) if num else None, "name": "",
                "src": ends.group("src").strip() if ends else "", "dst": ends.group("dst").strip() if ends else ""}
    m = _ADVANCE.match(t)
    if m:
        amount, name = _amount_and_name(m.group(1))
        return {"kind": "advance", "amount": amount, "name": name}
    m = _COLLECT.match(t)
    if m:
        amount, name = _amount_and_name(m.group(1))
        # «استلمت الطلب من أحمد» is talk; «حصّل من أحمد» with no amount is asked for it.
        if amount is not None or re.match(_LEAD + r"(?:تحصيل|حصل|حصلي)\s", t):
            return {"kind": "collection", "amount": amount, "name": name}
    m = _VENDOR.match(t)
    if m and _VENDOR_WORD.search(m.group(1)):   # «دفعنا 200 كهربا» is an expense (robot/entries.py)
        amount, name = _amount_and_name(_VENDOR_WORD.sub(" ", m.group(1), count=1).strip())
        return {"kind": "vendor_pay", "amount": amount, "name": name}
    m = _PAID_US.match(t)
    if m and re.search(_NUMBER, m.group(2)) and not _VENDOR_WORD.search(t):
        amount, _rest = _amount_and_name(m.group(2))
        _none, name = _amount_and_name(m.group(1))
        if name and amount is not None and len(name.split()) <= 4:
            return {"kind": "collection", "amount": amount, "name": name}
    return None


def is_request(text: str) -> bool:
    return parse(text) is not None


def _month_said(rest: str):
    """(month, year) of «شهر 10», «الشهر اللي فات», or this month when none is said."""
    from django.utils import timezone

    today = timezone.localdate()
    m = re.search(r"شهر\s+(\d{1,2})(?:\s+(?:سنه\s+)?(\d{4}))?", rest)
    if m and 1 <= int(m.group(1)) <= 12:
        month = int(m.group(1))
        year = int(m.group(2)) if m.group(2) else (today.year if month <= today.month else today.year - 1)
        return month, year
    if re.search(r"اللي فات|الماضي|السابق", rest):
        return (12, today.year - 1) if today.month == 1 else (today.month - 1, today.year)
    return today.month, today.year


def find_treasury(said: str, branch):
    """The branch's active treasury called `said` («خزنة الكاش», «البنك», its own name) — or None
    when none, or more than one, fits."""
    from inventory.models import Treasury

    want = _SAFE_WORDS.sub("", wakename.normalize(said)).strip()
    if not want:
        return None
    rows = list(Treasury.objects.filter(branch=branch, is_active=True).order_by("id"))
    named = [t for t in rows if wakename.normalize(t.name) in (want, wakename.normalize(said))]
    if len(named) == 1:
        return named[0]
    kind = _SAFE_TYPES.get(want)
    typed = [t for t in rows if kind and t.type == kind]
    if len(typed) == 1:
        return typed[0]
    part = [t for t in rows if want in wakename.normalize(t.name)
            or _SAFE_WORDS.sub("", wakename.normalize(t.name)) in want]
    return part[0] if len(part) == 1 else None


# ---------------------------------------------------------------------------
# Who's named
# ---------------------------------------------------------------------------

def _loose(name: str) -> str:
    """A name the way it's compared: normalized, a leading «ل» of «لمحمد» let go by the caller."""
    return wakename.normalize(name)


def find_employee(name: str):
    """(the active employee called `name`, how many are): the full name, or a part of it
    that only one has («محمد» with two Mohameds: asked for the whole name)."""
    from hr.models import Employee

    want = _loose(name)
    if len(want) < 2:
        return None, 0
    exact, partial = [], []
    for emp in Employee.objects.select_related("user").filter(is_active=True, user__is_active=True)[:2000]:
        full = _loose(emp.user.get_full_name() or "")
        names = {full, _loose(emp.user.first_name or ""), _loose(emp.user.username or "")} - {""}
        if want in names:
            exact.append(emp)
        elif full and all(w in full.split() for w in want.split()):
            partial.append(emp)
    found = exact or partial
    return (found[0] if len(found) == 1 else None), len(found)


def _person(kind: str, name: str):
    """(who, how many match, what they're called) for the name said — a second try without a
    leading «ل» («لمحمد علي»)."""
    from . import entries

    tries = [name] + ([name[1:]] if name.startswith("ل") and len(name) > 3 else [])
    for said in tries:
        if kind == "collection":
            who, count = entries.find_customer(said)
        elif kind == "vendor_pay":
            who = entries.find_vendor(said)
            count = 1 if who is not None else 0
        else:
            who, count = find_employee(said)
        if who is not None or count:
            return who, count
    return None, 0


def _money(value) -> str:
    from .services import spoken_price
    return spoken_price(value)


# ---------------------------------------------------------------------------
# Asked for by voice
# ---------------------------------------------------------------------------

_ACTION = {"collection": "collect", "vendor_pay": "vendor_pay", "advance": "advance", "return": "return",
           "transfer": "transfer", "payroll": "payroll"}
_WHO = {"collection": "العميل", "vendor_pay": "المورد", "advance": "الموظف"}


def voice_request(text: str, device, employee):
    """A collection, a vendor payment or an advance from speech: (reply, payload), or None."""
    from . import entries, permissions

    asked = parse(text)
    if asked is None:
        return None
    kind = asked["kind"]
    action = "payroll_pay" if kind == "payroll" and asked["pay"] else _ACTION[kind]
    if not permissions.employee_can(employee, action):
        return permissions.denial_message(action), {"action": "denied"}
    if kind == "return":
        return _ask_return(asked, device, employee)
    if kind == "transfer":
        return _ask_transfer(asked, device, employee)
    if kind == "payroll":
        return _ask_payroll(asked, device, employee)
    if asked["amount"] is None or not asked["name"]:
        return f"قولها كاملة بالمبلغ والاسم، مثلاً: {EXAMPLES[kind]}.", {"action": "entry_incomplete", "kind": kind}
    amount = asked["amount"]
    who, count = _person(kind, asked["name"])
    if who is None:
        if count > 1:
            return (f"فيه أكتر من واحد اسمه {asked['name']}. قولها تاني باسمه كامل زي ما هو متسجل.",
                    {"action": "entry_incomplete", "kind": kind})
        where = {"collection": "العملاء", "vendor_pay": "الموردين", "advance": "الموظفين"}[kind]
        return (f"مش لاقي {_WHO[kind]} «{asked['name']}» — اتأكد من الاسم زي ما هو متسجل في شاشة {where}.",
                {"action": "entry_incomplete", "kind": kind})

    if kind == "collection":
        owed = Decimal(str(who.balance or 0))
        if owed <= 0:
            return f"{who.name} مش عليه حاجة دلوقتي، فمسجلتش تحصيل.", {"action": "entry_incomplete", "kind": kind}
        if amount > owed:
            return (f"المبلغ أكبر من اللي على {who.name}: عليه {_money(owed)} بس. قول المبلغ الصح.",
                    {"action": "entry_incomplete", "kind": kind})
        summary = f"تحصيل {_money(amount)} من {who.name} (عليه {_money(owed)})"
        data = {"customer_id": who.pk, "amount": str(amount)}
    elif kind == "vendor_pay":
        owed = Decimal(str(who.balance or 0))
        if owed <= 0:
            return (f"مفيش حاجة علينا للمورد {who.name} دلوقتي. الدفعة المقدمة بتتسجل من شاشة الموردين.",
                    {"action": "entry_incomplete", "kind": kind})
        if amount > owed:
            return (f"المبلغ أكبر من اللي علينا للمورد {who.name}: {_money(owed)} بس. قول المبلغ الصح.",
                    {"action": "entry_incomplete", "kind": kind})
        summary = f"دفعة {_money(amount)} للمورد {who.name} (علينا له {_money(owed)})"
        data = {"vendor_id": who.pk, "amount": str(amount)}
    else:
        called = who.user.get_full_name() or who.user.username
        summary = f"سلفة {_money(amount)} للموظف {called}"
        data = {"employee_id": who.pk, "amount": str(amount)}
    reply = entries.ask(device, kind=kind, summary=summary, amount=amount, employee=employee, data=data)
    return reply, {"action": "entry_ask_branch", "kind": kind}


def _incomplete(kind: str, reply: str):
    return reply, {"action": "entry_incomplete", "kind": kind}


def _ask_return(asked, device, employee):
    from inventory.models import SaleInvoice
    from inventory.services.invoice_service import InvoiceService

    from . import entries

    if not asked["invoice"]:
        return _incomplete("return", f"قول رقم الفاتورة، مثلاً: {EXAMPLES['return']}.")
    invoice = SaleInvoice.objects.filter(pk=asked["invoice"]).select_related("customer", "branch").first()
    if invoice is None or invoice.is_return:
        return _incomplete("return", f"مش لاقي فاتورة بيع رقم {asked['invoice']}.")
    if invoice.status != "posted":
        return _incomplete("return", f"فاتورة {invoice.pk} لسه مش معتمدة، فمينفعش يتعمل لها مرتجع.")
    left = [row for row in InvoiceService.get_returnable_items(invoice) if row["remaining"] > 0]
    if not left:
        return _incomplete("return", f"فاتورة {invoice.pk} اترجعت كلها قبل كده.")
    chosen = left
    if asked["what"]:
        num = re.match(r"^(\d+)\s+(.+)$", asked["what"])
        qty, name = (int(num.group(1)), num.group(2)) if num else (None, asked["what"])
        words = [w for w in wakename.normalize(name).split() if w not in ("ال", "حته", "قطعه", "حتت")]
        match = [row for row in left
                 if words and all(w in wakename.normalize(row["item"].product.name) for w in words)]
        if len(match) != 1:
            names = "، ".join(row["item"].product.name for row in left[:5])
            return _incomplete("return", f"مش لاقي «{name}» في فاتورة {invoice.pk}. اللي فيها: {names}.")
        row = match[0]
        qty = row["remaining"] if qty is None else qty
        if qty < 1 or qty > row["remaining"]:
            return _incomplete("return", f"اللي ينفع يترجع من {row['item'].product.name} في الفاتورة دي "
                                         f"{row['remaining']} بس.")
        chosen = [{"item": row["item"], "remaining": qty}]
    said = " و".join(f"{row['remaining']} {row['item'].product.name}" for row in chosen[:6])
    who = invoice.customer_display_name
    summary = f"مرتجع فاتورة {invoice.pk}{f' بتاعة {who}' if who else ''}: {said}"
    data = {"invoice_id": invoice.pk, "amount": "0", "branch_id": invoice.branch_id,
            "items": [{"item_id": row["item"].pk, "quantity": row["remaining"]} for row in chosen]}
    reply = entries.ask(device, kind="return", summary=summary, amount=0, employee=employee, data=data, confirm=True)
    return reply, {"action": "entry_confirm", "kind": "return"}


def _ask_transfer(asked, device, employee):
    from . import entries

    if asked["amount"] is None or not asked["src"] or not asked["dst"]:
        return _incomplete("transfer", f"قولها كاملة، مثلاً: {EXAMPLES['transfer']}.")
    branch = device.branch
    src, dst = find_treasury(asked["src"], branch), find_treasury(asked["dst"], branch)
    if src is None or dst is None:
        from inventory.models import Treasury
        names = "، ".join(Treasury.objects.filter(branch=branch, is_active=True).values_list("name", flat=True)[:6])
        missing = asked["src"] if src is None else asked["dst"]
        return _incomplete("transfer", f"مش عارف أنهي خزنة «{missing}». خزن الفرع: {names}.")
    if src.pk == dst.pk:
        return _incomplete("transfer", "الخزنتين واحدة. قول من أنهي خزنة لأنهي خزنة.")
    if Decimal(str(src.balance or 0)) < asked["amount"]:
        return _incomplete("transfer", f"{src.name} فيها {_money(src.balance)} بس، فمينفعش أحوّل {_money(asked['amount'])}.")
    summary = f"تحويل {_money(asked['amount'])} من {src.name} إلى {dst.name}"
    reply = entries.ask(device, kind="transfer", summary=summary, amount=asked["amount"], employee=employee,
                        data={"amount": str(asked["amount"]), "src_id": src.pk, "dst_id": dst.pk,
                              "branch_id": getattr(branch, "pk", None)}, confirm=True)
    return reply, {"action": "entry_confirm", "kind": "transfer"}


def _ask_payroll(asked, device, employee):
    from hr.models import PayrollRun
    from hr.services.payroll_service import PayrollService

    from . import entries

    month, year = asked["month"], asked["year"]
    run = PayrollRun.objects.filter(period_month=month, period_year=year).first()
    if not asked["pay"]:
        try:
            run = PayrollService.generate_payroll(month, year, created_by=getattr(employee, "user", None))
        except ValidationError as exc:
            return _incomplete("payroll", _why(exc))
        return (f"حسبت مرتبات شهر {month}: {run.total_employees} موظف، الإجمالي {_money(run.total_gross)}، "
                f"الخصومات {_money(run.total_deductions)}، والصافي {_money(run.total_net)}. مفيش فلوس خرجت. "
                f"راجعها من شاشة المرتبات، ولما تحب قول «اصرف مرتبات شهر {month}».",
                {"action": "payroll_calculated", "month": month, "year": year, "net": float(run.total_net)})
    if run is None or run.status == "draft":
        return _incomplete("payroll", f"مرتبات شهر {month} لسه متحسبتش. قول «احسب مرتبات شهر {month}» الأول.")
    if run.status == "paid":
        return _incomplete("payroll", f"مرتبات شهر {month} اتصرفت قبل كده.")
    summary = f"صرف مرتبات شهر {month} سنة {year}: {run.total_employees} موظف، الصافي {_money(run.total_net)}"
    reply = entries.ask(device, kind="payroll", summary=summary, amount=run.total_net, employee=employee,
                        data={"amount": str(run.total_net), "run_id": run.pk, "net": str(run.total_net),
                              "branch_id": getattr(device.branch, "pk", None)}, confirm=True)
    return reply, {"action": "entry_confirm", "kind": "payroll"}


# ---------------------------------------------------------------------------
# Writing to the ERP
# ---------------------------------------------------------------------------

def _why(exc: ValidationError) -> str:
    return " ".join(str(m) for m in getattr(exc, "messages", None) or [str(exc)])


def record(entry, branch):
    """Write a held collection / vendor payment / advance on `branch` (raises entries.EntryError).
    Returns the words to say when they aren't the usual «تمام، سجلت …» — else ''."""
    from . import entries
    from .brain import talk

    amount = Decimal(str(entry.data["amount"]))
    if entry.kind in CONFIRMED:
        try:
            with transaction.atomic():
                return {"return": _return, "transfer": _transfer, "payroll": _pay_salaries}[entry.kind](entry)
        except ValidationError as exc:
            raise entries.EntryError(f"{_why(exc)} فمسجلتش حاجة.") from exc
    with transaction.atomic():
        treasury = entries.cash_treasury(branch, lock=True)
        if treasury is None:
            raise entries.EntryError(f"فرع {talk.branch_said(branch.name)} مالوش خزنة كاش، فمسجلتش حاجة.")
        try:
            if entry.kind == "collection":
                from inventory.models import Customer
                from inventory.services import customer_payments

                customer = Customer.objects.filter(pk=entry.data["customer_id"]).first()
                if customer is None:
                    raise entries.EntryError("العميل ده مبقاش موجود، فمسجلتش حاجة.")
                customer_payments.collect(customer, amount, treasury, note="🤖 بالصوت")
                return ""
            if entry.kind == "vendor_pay":
                from inventory.models import Vendor
                from inventory.services import vendor_payments

                vendor = Vendor.objects.select_for_update().filter(pk=entry.data["vendor_id"]).first()
                if vendor is None:
                    raise entries.EntryError("المورد ده مبقاش موجود، فمسجلتش حاجة.")
                if amount > Decimal(str(vendor.balance or 0)):      # paid meanwhile, from the screen
                    raise entries.EntryError(f"اللي علينا للمورد {vendor.name} بقى أقل من المبلغ، فمسجلتش حاجة.")
                vendor_payments.pay_vendor(vendor, amount, treasury, note="🤖 بالصوت")
                return ""
            if entry.kind == "advance":
                return _advance(entry, amount, treasury)
        except ValidationError as exc:
            raise entries.EntryError(f"{_why(exc)} فمسجلتش حاجة.") from exc
    raise entries.EntryError("النوع ده مش بيتسجل بالصوت.")


def _advance(entry, amount: Decimal, treasury) -> str:
    from hr.models import Employee
    from hr.services.advance_service import AdvanceService
    from hr.services.leave_service import is_company_admin

    from . import entries

    who = Employee.objects.select_related("user").filter(pk=entry.data["employee_id"]).first()
    if who is None:
        raise entries.EntryError("الموظف ده مبقاش موجود، فمسجلتش حاجة.")
    called = who.user.get_full_name() or who.user.username
    asker = entry.employee
    advance = AdvanceService.request_advance(who, amount, 1, reason="🤖 اتطلبت بالصوت من الروبوت")
    user = getattr(asker, "user", None)
    may_approve = asker is not None and (is_company_admin(user) or asker.is_hr_manager
                                         or getattr(user, "is_superuser", False))
    if not may_approve:
        return (f"سجلت طلب سلفة {_money(amount)} للموظف {called}، ومستني موافقة مدير الموارد البشرية "
                "من شاشة السلف. الفلوس مخرجتش من الخزنة لسه.")
    AdvanceService.approve_advance(advance.pk, asker, by_user=user)
    AdvanceService.disburse_advance(advance.pk, treasury_id=treasury.pk, by_user=user)
    return ""


def _return(entry) -> str:
    """The ERP's own return, as its screen posts it (inventory/views_lightning/sales.py)."""
    from inventory.models import SaleInvoice, Treasury
    from inventory.services.invoice_service import InvoiceService

    from . import entries

    invoice = SaleInvoice.objects.filter(pk=entry.data["invoice_id"]).select_related("branch").first()
    if invoice is None:
        raise entries.EntryError("الفاتورة دي مبقتش موجودة، فمسجلتش حاجة.")
    till = entries.cash_treasury(invoice.branch) if invoice.branch_id else None
    ret = InvoiceService.create_return_invoice(invoice, return_items=entry.data.get("items") or None,
                                               treasury=till, note="🤖 مرتجع بالصوت من الروبوت")
    refund = Decimal(str(ret.paid_amount or 0))
    if refund > 0:
        if ret.treasury_id is None:
            raise ValidationError("فرع الفاتورة مالوش خزنة كاش يترد منها المبلغ.")
        locked = Treasury.objects.select_for_update().get(pk=ret.treasury_id)
        if locked.balance < refund:
            raise ValidationError(f"رصيد {locked.name} ({_money(locked.balance)}) مش مكفي لرد {_money(refund)} للعميل.")
    ret.status = "posted"
    ret.save()                                   # the ERP's posting: stock back, the refund out
    entry.sale_invoice = ret
    entry.amount = ret.total_amount or 0
    entry.save(update_fields=["sale_invoice", "amount"])
    credit = max(Decimal(str(ret.total_amount or 0)) - refund, Decimal("0"))
    said = f"تمام، سجلت {entry.summary}، بقيمة {_money(ret.total_amount)}."
    if refund > 0:
        said += f" رد للعميل {_money(refund)} كاش من الخزنة."
    if credit > 0:
        said += f" واتخصم {_money(credit)} من اللي عليه."
    return said


def _transfer(entry) -> str:
    """Two linked movements, as the treasuries' screen writes them."""
    import uuid

    from inventory.models import FinancialTransaction, Treasury
    from inventory.services.accounting_service import TRANSFER_TAG

    from . import entries

    amount = Decimal(str(entry.data["amount"]))
    rows = {t.pk: t for t in Treasury.objects.select_for_update()
            .filter(pk__in=[entry.data["src_id"], entry.data["dst_id"]], is_active=True).order_by("pk")}
    src, dst = rows.get(entry.data["src_id"]), rows.get(entry.data["dst_id"])
    if src is None or dst is None or src.branch_id != dst.branch_id:
        raise entries.EntryError("خزنة من الاتنين اتقفلت، فمسجلتش حاجة.")
    if Decimal(str(src.balance or 0)) < amount:
        raise entries.EntryError(f"{src.name} فيها {_money(src.balance)} بس، فمسجلتش حاجة.")
    ref = f"{TRANSFER_TAG}{uuid.uuid4().hex[:8]}]"
    FinancialTransaction.objects.create(treasury=src, transaction_type="out", amount=amount,
                                        description=f"{ref} تحويل إلى {dst.name}")
    FinancialTransaction.objects.create(treasury=dst, transaction_type="in", amount=amount,
                                        description=f"{ref} تحويل من {src.name}")
    return f"تمام، حوّلت {_money(amount)} من {src.name} إلى {dst.name}."


def _pay_salaries(entry) -> str:
    from hr.models import PayrollRun
    from hr.services.payroll_service import PayrollService

    from . import entries

    run = PayrollRun.objects.select_for_update().filter(pk=entry.data["run_id"]).first()
    if run is None or run.status == "paid":
        raise entries.EntryError("المرتبات دي اتصرفت أو اتمسحت، فمسجلتش حاجة.")
    if Decimal(str(run.total_net)) != Decimal(str(entry.data.get("net", run.total_net))):
        raise entries.EntryError("كشف المرتبات اتغيّر بعد ما قلت الرقم. قول «اصرف المرتبات» تاني علشان أقولك الصافي الجديد.")
    if run.status == "calculated":
        PayrollService.approve_payroll(run.pk, approved_by=getattr(entry.employee, "user", None))
    run = PayrollService.disburse_payroll(run.pk)
    return (f"تمام، صرفت مرتبات شهر {run.period_month}: {run.total_employees} موظف بصافي {_money(run.total_net)}.")
