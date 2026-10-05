"""
robot/entries.py — invoices and expenses the robot records, always on a branch
someone names.

Before ANY invoice or expense is written, the robot asks which branch it
belongs to. By voice:

    «يا موس، سجل مصروف 200 جنيه بنزين»
        → «تمام: مصروف 200 جنيه (بنزين). أسجله على أنهي فرع؟ …»
    «المعادي»   → recorded on the المعادي branch, paid from its cash treasury
    «هنا»       → recorded on the robot's own branch
    «الغي»      → nothing is recorded

Purchases work the same way («اشترينا 3 فلتر زيت من الأمل بسعر 150 كاش»). The
`/sale/` endpoint asks too: without a `branch_id` it answers with the question
and the branch list instead of creating the invoice.

Every request becomes a `RobotEntry`: pending until answered, then done (with
links to the invoice/transaction it created), cancelled, failed (with the
reason) or expired. The dashboard lists these, so it shows only what the robot
did and on which branch.

Money rules match the ERP screens: an expense or a cash purchase is paid from
the chosen branch's active cash treasury and is refused when that treasury
can't cover it; a cash sale is refused when the branch has no cash treasury
(otherwise the money would be recorded nowhere).
"""

from __future__ import annotations

import re
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone

from . import wakename

PENDING_MINUTES = 3

# Answers that mean "the branch the robot is in".
_HERE = {"هنا", "هنا يا موس", "الفرع ده", "الفرع دا", "الفرع هنا", "نفس الفرع",
         "فرعنا", "عندنا", "here", "this branch"}
_CANCEL = {"الغي", "الغيها", "الغيه", "لا", "لأ", "بلاش", "خلاص لا", "cancel", "no"}

_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩٫", "0123456789.")
_NUMBER = r"(\d+(?:[.,]\d+)?)"
_CURRENCY = r"(?:\s*(?:جنيه|جنية|جنيهات|ج\.?م|ج|egp|pound|pounds))?"


class EntryError(Exception):
    """A request the robot can't record — the message is spoken as-is."""


# ---------------------------------------------------------------------------
# Branches
# ---------------------------------------------------------------------------

def all_branches() -> list:
    from inventory.models import Branch
    return list(Branch.objects.order_by("name"))


def branch_by_id(value):
    """The Branch with this id, or None for a missing/bad id."""
    from inventory.models import Branch
    try:
        return Branch.objects.filter(pk=int(value)).first()
    except (TypeError, ValueError):
        return None


def _branch_key(name: str) -> str:
    t = wakename.normalize(name)
    return re.sub(r"^(فرع|الفرع)\s+", "", t).strip()


def match_branch(text: str, branches: list, here):
    """The branch named in `text`, `here` for «هنا», or None.

    Matches whole words of the branch name, with or without «فرع». When a
    longer name contains a shorter one ("مدينة نصر 2" vs "مدينة نصر"), the
    longest match wins.
    """
    t = wakename.normalize(text)
    if not t:
        return None
    if t in {wakename.normalize(h) for h in _HERE}:
        return here
    padded = " " + re.sub(r"^(فرع|الفرع)\s+", "", t) + " "
    best, best_len = None, 0
    for b in branches:
        key = _branch_key(b.name)
        if key and f" {key} " in padded and len(key) > best_len:
            best, best_len = b, len(key)
    return best


def is_cancel(text: str) -> bool:
    return wakename.normalize(text) in {wakename.normalize(c) for c in _CANCEL}


def branch_question(device, branches=None) -> str:
    """«أسجلها على أنهي فرع؟» with the branch names to choose from."""
    branches = all_branches() if branches is None else branches
    here = getattr(device.branch, "name", "")
    if len(branches) <= 1:
        return f"أسجلها على فرع {here}؟ قول «هنا» أو «الغي»."
    names = "، ".join(b.name for b in branches)
    return (f"أسجلها على أنهي فرع؟ الفروع: {names}. "
            f"قول اسم الفرع، أو «هنا» لفرع {here}، أو «الغي».")


def branch_choices(device) -> dict:
    """The /sale/ reply when no branch was chosen yet."""
    branches = all_branches()
    return {
        "needs_branch": True,
        "question": branch_question(device, branches),
        "branches": [{"id": b.pk, "name": b.name} for b in branches],
        "suggested_branch_id": device.branch_id,
    }


# ---------------------------------------------------------------------------
# Parsing what was said
# ---------------------------------------------------------------------------

_EXPENSE_RE = re.compile(
    r"^(?:(?:سجل|سجّل|سجلي|اكتب|ضيف|اضف|أضف)\s+)?(?:مصروف|مصاريف|صرف)\s+(.+)$")
_SPENT_RE = re.compile(r"^(?:صرفنا|صرفت|دفعنا|دفعت)\s+(.+)$")
_PURCHASE_RE = re.compile(
    r"^(?:(?:سجل|سجّل|سجلي|اعمل|اكتب)\s+)?"
    r"(?:فاتورة شراء|فاتوره شراء|شراء|مشتريات|اشترينا|اشتريت)\s+(.+)$")


def _to_decimal(s: str):
    try:
        d = Decimal(s.replace(",", "."))
    except (InvalidOperation, AttributeError):
        return None
    return d if d > 0 else None


def parse_expense(text: str):
    """{"amount", "description"} from «مصروف 200 جنيه بنزين», else None.

    Returns {"amount": None, ...} when it's clearly an expense but no amount
    was heard, so the robot can ask for it.
    """
    t = (text or "").translate(_DIGITS).strip()
    m = _EXPENSE_RE.match(t) or _SPENT_RE.match(t)
    if not m:
        return None
    rest = m.group(1)
    num = re.search(_NUMBER + _CURRENCY, rest)
    amount = _to_decimal(num.group(1)) if num else None
    desc = (rest[:num.start()] + " " + rest[num.end():]) if num else rest
    desc = re.sub(r"^\s*(?:على|علي|عشان|في|ل|لل)\s+", "", desc.strip())
    desc = " ".join(desc.split()).strip(" ،,.-") or "مصروف"
    return {"amount": amount, "description": desc[:200]}


def parse_purchase(text: str):
    """Parts of «اشترينا 3 فلتر زيت من الأمل بسعر 150 كاش», else None.

    {"quantity", "query", "vendor", "unit_cost", "cash"}. «بسعر X» is the
    price of one piece; «بإجمالي X» / «بـ X الكل» is the whole bill.
    """
    t = (text or "").translate(_DIGITS).strip()
    m = _PURCHASE_RE.match(t)
    if not m:
        return None
    rest = f" {m.group(1)} "

    cash = bool(re.search(r"\s(كاش|نقدي|نقدا|نقداً|cash)\s", rest))
    rest = re.sub(r"\s(كاش|نقدي|نقدا|نقداً|cash|آجل|اجل|أجل|على الحساب)\s", " ", rest)

    unit_cost, total = None, None
    pm = re.search(r"\s(?:باجمالي|بإجمالي|اجمالي|إجمالي|الاجمالي|الإجمالي)\s*" + _NUMBER
                   + _CURRENCY, rest)
    if pm:
        total = _to_decimal(pm.group(1))
    else:
        pm = re.search(r"\s(?:بسعر|السعر|سعر|بـ|ب)\s*" + _NUMBER + _CURRENCY
                       + r"(\s+(?:الكل|للكل|كلهم))?", rest)
        if pm:
            value = _to_decimal(pm.group(1))
            if pm.group(2):
                total = value
            else:
                unit_cost = value
    if pm:
        rest = rest[:pm.start()] + " " + rest[pm.end():]

    vendor = None
    vm = re.search(r"\s(?:من عند|من)\s+(?:المورد\s+)?(.+?)\s*$", rest)
    if vm:
        vendor = vm.group(1).strip(" ،,.-")
        rest = rest[:vm.start()]

    rest = rest.strip()
    qty = 1
    qm = re.match(r"^(\d+)\s+(?:قطعة|قطع|حتة|حتت|عدد\s+)?\s*(.*)$", rest)
    if qm:
        qty = int(qm.group(1))
        rest = qm.group(2)
    query = rest.strip(" ،,.-") or None

    if total is not None and qty > 0:
        unit_cost = (total / qty).quantize(Decimal("0.01"))
    return {"quantity": qty, "query": query, "vendor": vendor,
            "unit_cost": unit_cost, "cash": cash}


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------

def find_vendor(name: str):
    """A Vendor whose name matches what was said, or None."""
    from inventory.models import Vendor
    if not name:
        return None
    exact = Vendor.objects.filter(name__iexact=name.strip()).first()
    if exact:
        return exact
    contains = Vendor.objects.filter(name__icontains=name.strip()).order_by("id").first()
    if contains:
        return contains
    # STT spells names loosely (ة/ه, أ/ا): compare normalized forms.
    want = wakename.normalize(name)
    for v in Vendor.objects.order_by("id")[:500]:
        if want and want in wakename.normalize(v.name):
            return v
    return None


def expense_category(description: str):
    """(category, salaries?) — the expense category named in the description.

    Falls back to the «أخرى» category. Salaries are not recorded by voice:
    the ERP needs the employee who's being paid.
    """
    from inventory.models import ExpenseCategory
    words = f" {wakename.normalize(description)} "
    best = None
    for c in ExpenseCategory.objects.all():
        key = wakename.normalize(c.name)
        if key and f" {key} " in words and (best is None or len(key) > len(best[1])):
            best = (c, key)
    if best:
        return best[0], best[0].system_key == "salaries"
    other = ExpenseCategory.objects.filter(system_key="other").first()
    return other, False


def cash_treasury(branch, *, lock=False):
    """The branch's active cash treasury, or None."""
    from inventory.models import Treasury
    qs = Treasury.objects.filter(branch=branch, type="cash", is_active=True).order_by("id")
    if lock:
        qs = qs.select_for_update()
    return qs.first()


# ---------------------------------------------------------------------------
# The pending → answered flow
# ---------------------------------------------------------------------------

def pending_for(device):
    """This device's open request, expiring one that waited too long."""
    from .models import RobotEntry
    entry = (RobotEntry.objects.filter(device=device, status="pending")
             .order_by("-created_at").first())
    if entry is None:
        return None
    if entry.created_at < timezone.now() - timedelta(minutes=PENDING_MINUTES):
        RobotEntry.objects.filter(device=device, status="pending").update(
            status="expired", completed_at=timezone.now())
        return None
    return entry


def is_answer(device, text: str) -> bool:
    """True when `text` names a branch while the question is open, so it
    needs no «يا موس».

    Only real branch names count: «لا» or «هنا» said to someone else nearby
    must not cancel or place the entry. Those still work within the 20 s
    conversation window, or with the name.
    """
    if pending_for(device) is None:
        return False
    return match_branch(text, all_branches(), None) is not None


def ask(device, *, kind: str, data: dict, summary: str, amount, employee=None) -> str:
    """Hold a request until a branch is named; returns the spoken question."""
    from .models import RobotEntry
    RobotEntry.objects.filter(device=device, status="pending").update(
        status="cancelled", completed_at=timezone.now(),
        message="اتلغت لما اتطلبت عملية جديدة")
    RobotEntry.objects.create(
        device=device, kind=kind, data=data, summary=summary[:255],
        amount=amount or 0, employee=employee,
    )
    return f"تمام: {summary}. {branch_question(device)}"


def answer(entry, text: str):
    """Handle a reply to the branch question.

    Returns (reply, payload) when `text` was an answer (branch or cancel), or
    None when it wasn't — the caller then treats it as a normal question.
    """
    if is_cancel(text):
        _finish(entry, "cancelled", message="اتلغت بالصوت")
        return "ماشي، لغيتها ومسجلتش حاجة.", {"action": "entry_cancelled", "entry_id": entry.pk}
    branch = match_branch(text, all_branches(), entry.device.branch)
    if branch is None:
        return None
    try:
        record(entry, branch)
    except EntryError as exc:
        _finish(entry, "failed", branch=branch, message=str(exc))
        return str(exc), {"action": "entry_failed", "entry_id": entry.pk}
    return (f"تمام، سجلت {entry.summary} على فرع {branch.name}.",
            {"action": "entry_done", "entry_id": entry.pk, "branch": branch.name})


def _finish(entry, status, *, branch=None, message="", **links):
    entry.status = status
    entry.completed_at = timezone.now()
    if branch is not None:
        entry.branch = branch
    entry.message = message[:255]
    fields = ["status", "completed_at", "branch", "message"]
    for k, v in links.items():
        setattr(entry, k, v)
        fields.append(k)
    entry.save(update_fields=fields)


def record(entry, branch):
    """Write the held request to the books on `branch` (raises EntryError)."""
    if entry.kind == "expense":
        tx = record_expense(branch=branch, amount=Decimal(str(entry.data["amount"])),
                            description=entry.data.get("description", ""),
                            category_id=entry.data.get("category_id"))
        _finish(entry, "done", branch=branch, transaction=tx)
    elif entry.kind == "purchase":
        invoice = record_purchase(
            branch=branch, product_id=entry.data["product_id"],
            vendor_id=entry.data["vendor_id"], quantity=int(entry.data["quantity"]),
            unit_cost=Decimal(str(entry.data["unit_cost"])), cash=bool(entry.data.get("cash")))
        _finish(entry, "done", branch=branch, purchase_invoice=invoice)
    else:
        raise EntryError("النوع ده مش بيتسجل بالصوت.")


# ---------------------------------------------------------------------------
# Writing to the ERP
# ---------------------------------------------------------------------------

def record_expense(*, branch, amount: Decimal, description: str, category_id=None):
    """An operating expense paid from the branch's cash treasury."""
    from inventory.models import ExpenseCategory, FinancialTransaction
    with transaction.atomic():
        treasury = cash_treasury(branch, lock=True)
        if treasury is None:
            raise EntryError(f"فرع {branch.name} مالوش خزنة كاش، فمسجلتش المصروف.")
        if (treasury.balance or Decimal("0")) < amount:
            raise EntryError(f"رصيد خزنة فرع {branch.name} مش مكفي "
                             f"(المتاح {treasury.balance:.0f} جنيه)، فمسجلتش المصروف.")
        category = (ExpenseCategory.objects.filter(pk=category_id).first()
                    if category_id else None)
        return FinancialTransaction.objects.create(
            treasury=treasury, transaction_type="out", amount=amount,
            description=f"🤖 {description}"[:255], category=category,
        )


def record_purchase(*, branch, product_id, vendor_id, quantity: int,
                    unit_cost: Decimal, cash: bool):
    """A posted purchase invoice received at `branch`.

    Cash → paid in full from the branch's cash treasury (refused when it can't
    cover it). Otherwise it's on credit and the vendor's balance grows. Posting
    runs the ERP's own pipeline (stock in, average cost, ledger).
    """
    from inventory.models import Product, PurchaseInvoice, PurchaseInvoiceItem, Vendor
    with transaction.atomic():
        product = Product.objects.get(pk=product_id)
        vendor = Vendor.objects.get(pk=vendor_id)
        total = (unit_cost * quantity).quantize(Decimal("0.01"))
        treasury = None
        if cash:
            treasury = cash_treasury(branch, lock=True)
            if treasury is None:
                raise EntryError(f"فرع {branch.name} مالوش خزنة كاش — "
                                 "قول «آجل» لو هتتسجل على حساب المورد.")
            if (treasury.balance or Decimal("0")) < total:
                raise EntryError(f"رصيد خزنة فرع {branch.name} مش مكفي "
                                 f"(المتاح {treasury.balance:.0f} جنيه).")
        invoice = PurchaseInvoice.objects.create(vendor=vendor, branch=branch, status="draft")
        PurchaseInvoiceItem.objects.create(invoice=invoice, product=product,
                                           quantity=quantity, cost_price=unit_cost)
        invoice.update_total()
        if treasury is not None:
            invoice.treasury = treasury
            invoice.paid_amount = invoice.total_amount
            invoice.save(update_fields=["treasury", "paid_amount"])
        invoice.status = "posted"
        invoice.save(update_fields=["status"])
        invoice.refresh_from_db()
        return invoice


def log_sale(*, device, branch, invoice, employee=None, summary=""):
    """Record a /sale/ invoice as a robot entry (for the dashboard)."""
    from .models import RobotEntry
    return RobotEntry.objects.create(
        device=device, kind="sale", status="done", branch=branch, employee=employee,
        sale_invoice=invoice, amount=invoice.total_amount or 0,
        summary=summary[:255], completed_at=timezone.now(),
    )


# ---------------------------------------------------------------------------
# Voice requests
# ---------------------------------------------------------------------------

def voice_request(text: str, device, employee):
    """Start an expense/purchase from speech. (reply, payload) or None.

    Checks permission and validates everything that doesn't depend on the
    branch, so the branch answer only has to pick where it goes.
    """
    from . import permissions, services

    exp = parse_expense(text)
    if exp is not None:
        if not permissions.employee_can(employee, "expense"):
            return permissions.denial_message("expense"), {"action": "denied"}
        if exp["amount"] is None:
            return ("قول المصروف بالمبلغ، مثلاً: «سجل مصروف 200 جنيه بنزين».",
                    {"action": "entry_needs_amount"})
        category, salaries = expense_category(exp["description"])
        if salaries:
            return ("المرتبات بتتسجل من شاشة المصروفات عشان محتاجة اسم الموظف.",
                    {"action": "entry_salaries"})
        summary = f"مصروف {exp['amount']:.0f} جنيه ({exp['description']})"
        reply = ask(device, kind="expense", summary=summary, amount=exp["amount"],
                    employee=employee,
                    data={"amount": str(exp["amount"]), "description": exp["description"],
                          "category_id": getattr(category, "pk", None)})
        return reply, {"action": "entry_ask_branch", "kind": "expense"}

    pur = parse_purchase(text)
    if pur is not None:
        if not permissions.employee_can(employee, "purchase"):
            return permissions.denial_message("purchase"), {"action": "denied"}
        if not pur["query"]:
            return ("قول الصنف، مثلاً: «اشترينا 3 فلتر زيت من الأمل بسعر 150».",
                    {"action": "entry_incomplete"})
        product = services.find_product(pur["query"])
        if product is None:
            return (f"مش لاقي «{pur['query']}» في الأصناف — قول رقم القطعة.",
                    {"action": "entry_incomplete"})
        if not pur["vendor"]:
            return "من أنهي مورد؟ قولها كاملة: «… من <اسم المورد> بسعر …».", {
                "action": "entry_incomplete"}
        vendor = find_vendor(pur["vendor"])
        if vendor is None:
            return (f"مش لاقي المورد «{pur['vendor']}» — ضيفه من شاشة الموردين الأول.",
                    {"action": "entry_incomplete"})
        if not pur["unit_cost"]:
            return "بكام؟ قولها كاملة: «… بسعر 150».", {"action": "entry_incomplete"}
        total = pur["unit_cost"] * pur["quantity"]
        pay = "كاش" if pur["cash"] else "آجل على المورد"
        summary = (f"شراء {pur['quantity']} {product.name} من {vendor.name} "
                   f"بإجمالي {total:.0f} جنيه ({pay})")
        reply = ask(device, kind="purchase", summary=summary, amount=total,
                    employee=employee,
                    data={"product_id": product.pk, "vendor_id": vendor.pk,
                          "quantity": pur["quantity"], "unit_cost": str(pur["unit_cost"]),
                          "cash": pur["cash"]})
        return reply, {"action": "entry_ask_branch", "kind": "purchase"}
    return None
