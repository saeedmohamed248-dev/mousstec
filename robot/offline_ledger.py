"""
robot/offline_ledger.py — what the shop sold and spent on «مخ موس»'s counter
page while the internet was down, written to the books when it's back.

The rules agreed with the owner:

* cash only — offline nobody can check a customer's account;
* a part that's out isn't sold (the brain checks its own count, robot/brain/
  ledger.py);
* who: the employee's PIN on the counter page (robot/brain/pins.py) — the face
  too, once the camera is on the mini PC;
* an expense is recorded at once; the manager reviews or edits it later.

Each event arrives through /sync/push/ (kind «sale» / «expense», once per
client_uid) and becomes a RobotEntry the robot's page lists. It's written by
the ERP's own pipeline — `services.create_robot_sale_lines`,
`entries.record_expense` — so stock, treasury and journal follow the same
rules as at the counter. When the books can't take it as it is any more (the
part was sold online meanwhile, the role changed, the treasury can't cover the
expense), nothing is forced and nothing is lost: the entry is kept as failed
with the reason, and the manager gets an alert. The money was really taken or
paid, so it must neither vanish nor be booked wrong.
"""

from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from . import entries, permissions

logger = logging.getLogger("mouss_tec_core")


class Review(Exception):
    """Can't be booked as it is: kept for the manager, with this reason."""


def _when(payload) -> str:
    try:
        at = datetime.fromtimestamp(float(payload.get("at")), tz=timezone.get_current_timezone())
    except (TypeError, ValueError, OverflowError, OSError):
        return ""
    return at.strftime("%Y-%m-%d %H:%M")


def _employee(payload, action: str):
    from hr.models import Employee

    employee = Employee.objects.filter(pk=payload.get("employee_id")).select_related("user").first()
    if employee is None:
        raise Review("الموظف اللي سجّلها مش موجود.")
    if not getattr(employee.user, "is_active", True) or not permissions.employee_can(employee, action):
        raise Review(f"{employee.name} مش مسموحله دلوقتي بـ«{permissions.action_label(action)}».")
    return employee


def _review(device, *, kind: str, payload: dict, summary: str, amount, reason: str, employee=None):
    from .models import RobotAlert, RobotEntry

    entry = RobotEntry.objects.create(
        device=device, kind=kind, status="failed", data=payload, summary=summary[:255],
        amount=amount or 0, branch=device.branch, employee=employee,
        message=f"محتاجة مراجعة: {reason}"[:255], completed_at=timezone.now())
    RobotAlert.objects.create(device=device, kind="other", message=(
        f"🧾 {summary} — اتعملت والنت فاصل ومحتاجة مراجعة: {reason}")[:255])
    logger.warning("robot offline %s #%s needs review: %s", kind, entry.pk, reason)
    return entry


def apply_sale(device, payload: dict):
    """A cash sale from the counter page → a posted invoice (or a review)."""
    from inventory.models import Customer, Product, SaleInvoice
    from inventory.models.customers import WALK_IN_PHONE

    from . import services
    from .models import RobotEntry

    items = payload.get("items") if isinstance(payload.get("items"), list) else []
    receipt = str(payload.get("receipt") or "")[:40]
    # 🐛 [FIX]: int(qty) here was outside the review guard: a malformed line
    # failed the whole event instead of being kept for the manager.
    said = "، ".join(f"{str(i.get('qty'))[:6]} {i.get('name') or i.get('part_number')}" for i in items[:6]
                     if isinstance(i, dict))
    summary = f"بيع كاش والنت فاصل {receipt}: {said}".strip()
    total = Decimal("0")
    employee = None
    try:
        employee = _employee(payload, "sale")
        if not items:
            raise Review("الفاتورة فاضية.")
        lines = []
        for item in items:
            if not isinstance(item, dict):
                raise Review("سطر بايظ في الفاتورة.")
            pn = str(item.get("part_number") or "").strip()
            product = Product.objects.filter(part_number=pn).first() if pn else None
            if product is None:
                raise Review(f"الصنف {pn or '؟'} مش موجود.")
            try:
                qty = int(item.get("qty"))
                price = Decimal(str(item.get("unit_price")))
            except (TypeError, ValueError, InvalidOperation):
                raise Review(f"كمية أو سعر بايظ لـ {product.name}.")
            if qty < 1 or not price.is_finite() or price <= 0:
                raise Review(f"كمية أو سعر بايظ لـ {product.name}.")
            lines.append((product, qty, price))
            total += price * qty
        if entries.cash_treasury(device.branch) is None:
            raise Review(f"فرع {device.branch.name} مالوش خزنة كاش.")
        changed = [p.name for p, _q, price in lines if Decimal(str(p.retail_price or 0)) != price]
        walk_in, _ = Customer.get_or_create_by_phone(WALK_IN_PHONE, defaults={"name": "عميل نقدي"})
        note = f"🤖 بيع كاش والنت فاصل ({receipt}) — اتعمل {_when(payload)} واتسجّل لما النت رجع."
        try:
            with transaction.atomic():
                invoice = services.create_robot_sale_lines(
                    lines=lines, branch=device.branch, customer=walk_in, payment="cash", notes=note)
                name = str(payload.get("customer_name") or "").strip()
                if name:
                    invoice.set_walk_in_name(name)
                    SaleInvoice.objects.filter(pk=invoice.pk).update(notes=invoice.notes)
        except ValidationError as exc:      # the stock isn't there now (sold online meanwhile)
            raise Review("المخزون مكفاش وقت المزامنة: " + "؛ ".join(exc.messages))
        except IntegrityError:              # the same, caught by the database's own rule
            raise Review("المخزون مكفاش وقت المزامنة.")
        except ValueError as exc:
            raise Review(str(exc))
    except Review as why:
        return _review(device, kind="sale", payload=payload, summary=summary, amount=total,
                       reason=str(why), employee=employee)
    message = ("السعر اتغيّر من ساعتها: " + "، ".join(changed))[:255] if changed else ""
    return RobotEntry.objects.create(
        device=device, kind="sale", status="done", data=payload, summary=summary[:255],
        amount=invoice.total_amount or 0, branch=device.branch, employee=employee,
        sale_invoice=invoice, message=message, completed_at=timezone.now())


def apply_expense(device, payload: dict):
    """An expense from the counter page → paid from the branch's cash (or a review)."""
    from .models import RobotEntry

    description = str(payload.get("description") or "").strip()[:200]
    try:
        amount = Decimal(str(payload.get("amount")))
    except (TypeError, ValueError, InvalidOperation):
        amount = Decimal("0")
    if not amount.is_finite():
        amount = Decimal("0")
    summary = f"مصروف والنت فاصل {amount:.2f} جنيه ({description})"
    employee = None
    try:
        employee = _employee(payload, "expense")
        if amount <= 0 or not description:
            raise Review("مبلغ أو بيان بايظ.")
        category, salaries = entries.expense_category(description)
        if salaries:
            raise Review("المرتبات بتتسجّل من شاشة الموظفين، مش من الروبوت.")
        try:
            with transaction.atomic():
                tx = entries.record_expense(
                    branch=device.branch, amount=amount,
                    description=f"{description} (والنت فاصل {_when(payload)})",
                    category_id=getattr(category, "pk", None))
        except entries.EntryError as exc:
            raise Review(str(exc))
    except Review as why:
        return _review(device, kind="expense", payload=payload, summary=summary, amount=amount,
                       reason=str(why), employee=employee)
    return RobotEntry.objects.create(
        device=device, kind="expense", status="done", data=payload, summary=summary[:255],
        amount=amount, branch=device.branch, employee=employee, transaction=tx,
        completed_at=timezone.now())


def staff_for(branch) -> list:
    """Who may use the counter page on this branch's robot, and for what —
    the PIN's hash only (robot/brain/pins.py)."""
    from .models import RobotStaffPin

    out = []
    for pin in RobotStaffPin.objects.select_related("employee__user"):
        employee = pin.employee
        user = employee.user
        if user is not None and not user.is_active:
            continue
        profile = getattr(user, "employee_profile", None)
        home = getattr(profile, "branch_id", None)
        if home and branch is not None and home != branch.pk:
            continue
        can = [a for a in ("sale", "expense", "stock_take") if permissions.employee_can(employee, a)]
        if can:
            out.append({"id": employee.pk, "name": employee.name, "pin": pin.pin_hash, "can": can})
    return out
