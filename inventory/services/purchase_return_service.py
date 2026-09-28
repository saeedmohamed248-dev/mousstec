"""
↩️ Purchase return (مرتجع مشتريات) — send received parts back to the vendor.

The system could receive goods from a vendor and reverse a whole purchase
invoice (only while none of it was sold), but there was no way to return
*some* parts — a defective alternator, a wrong-model filter — to the vendor.
``InventoryMovement`` even had a ``purchase_return`` reason nobody used.

One call does the whole cycle, atomically:

* stock leaves the receiving branch (movement reason ``purchase_return``) and
  the product's weighted average cost is unwound at the unit's landed cost;
* the vendor's payable drops by the vendor price of what went back;
* optionally the vendor refunds cash into a treasury (a normal ``in``
  movement → Dr Cash / Cr AP), the rest stays as a credit on his account;
* the ledger gets  Dr AP (vendor price) + Dr import expense (the landed
  costs spent on the returned units, now sunk) / Cr Inventory (landed value).
"""
from __future__ import annotations

import logging
from decimal import Decimal, ROUND_HALF_UP

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F

logger = logging.getLogger('mouss_tec_core')

TWO = Decimal('0.01')


def _q(v):
    return Decimal(str(v or 0)).quantize(TWO, rounding=ROUND_HALF_UP)


@transaction.atomic
def return_to_vendor(invoice, lines, *, refund_treasury=None, refund_amount=None,
                     note='', user=None):
    """
    Return parts of a received purchase invoice to its vendor.

    Args:
        invoice: PurchaseInvoice (must be received / applied).
        lines: iterable of ``(item_id, quantity)`` — quantities to send back.
        refund_treasury: Treasury the vendor's cash refund goes into (optional).
        refund_amount: cash the vendor paid back now (≤ vendor value returned).
        note: free text for the movement / journal narration.
        user: acting user (audit).

    Returns a dict summary. Raises ValidationError on any invalid input —
    nothing is written in that case.
    """
    from inventory.models import (
        FinancialTransaction, Inventory, InventoryMovement, Product, PurchaseInvoice,
        PurchaseInvoiceItem, Treasury, Vendor,
    )
    from inventory.services.accounting_service import AccountingService

    inv = PurchaseInvoice.objects.select_for_update().select_related('vendor', 'branch').get(pk=invoice.pk)
    if inv.status != 'posted' or not inv.is_applied:
        raise ValidationError("المرتجع بيتعمل على فاتورة شراء مستلمة (معتمدة) بس.")

    wanted = {}
    for item_id, qty in lines:
        try:
            qty = int(qty)
        except (TypeError, ValueError):
            raise ValidationError("كمية المرتجع لازم تكون رقم صحيح.")
        if qty < 0:
            raise ValidationError("كمية المرتجع مينفعش تبقى بالسالب.")
        if qty:
            wanted[int(item_id)] = wanted.get(int(item_id), 0) + qty
    if not wanted:
        raise ValidationError("اختار صنف واحد على الأقل وكمية المرتجع بتاعته.")

    items = {it.pk: it for it in PurchaseInvoiceItem.objects.select_for_update()
             .select_related('product').filter(invoice=inv, pk__in=wanted)}
    if len(items) != len(wanted):
        raise ValidationError("صنف مش تابع للفاتورة دي.")

    vendor_value = landed_value = Decimal('0')
    done = []
    for item_id, qty in wanted.items():
        item = items[item_id]
        returnable = item.quantity - item.returned_quantity
        if qty > returnable:
            raise ValidationError(
                f"«{item.product.name}»: المتاح للإرجاع {returnable} بس "
                f"(المستلم {item.quantity}، اترجع قبل كده {item.returned_quantity}).")
        row = (Inventory.objects.select_for_update()
               .filter(product=item.product, branch=inv.branch).first())
        on_hand = row.quantity if row else 0
        if on_hand < qty:
            raise ValidationError(
                f"«{item.product.name}»: الموجود في المخزن {on_hand} بس — مينفعش ترجّع {qty}.")

        unit_landed = Decimal(str(item.effective_unit_cost))
        line_vendor = _q(Decimal(qty) * Decimal(str(item.cost_price)))
        line_landed = _q(Decimal(qty) * unit_landed)
        vendor_value += line_vendor
        landed_value += line_landed

        before = row.quantity
        row.quantity = before - qty
        row.save(update_fields=['quantity'])

        # Unwind the weighted average at the landed unit cost it came in at.
        prod = Product.objects.select_for_update().get(pk=item.product_id)
        remaining = prod.total_inventory_qty
        if remaining > 0:
            total_before = remaining + qty
            new_avg = ((Decimal(total_before) * Decimal(str(prod.average_cost)))
                       - (Decimal(qty) * unit_landed)) / Decimal(remaining)
            prod.average_cost = max(new_avg, Decimal('0')).quantize(TWO, rounding=ROUND_HALF_UP)
            prod.save(update_fields=['average_cost'])

        InventoryMovement.objects.create(
            product=item.product, branch=inv.branch, reason='purchase_return',
            quantity_change=-qty, quantity_before=before, quantity_after=row.quantity,
            reference_type='PurchaseInvoice', reference_id=inv.id,
            note=(f"مرتجع للمورد {inv.vendor.name} من فاتورة #{inv.id}" + (f" — {note}" if note else ""))[:255],
            created_by=user if getattr(user, 'is_authenticated', False) else None,
        )
        PurchaseInvoiceItem.objects.filter(pk=item.pk).update(returned_quantity=F('returned_quantity') + qty)
        done.append({'item_id': item.pk, 'product': item.product.name, 'quantity': qty,
                     'value': line_vendor})

    refund = _q(refund_amount) if refund_amount else Decimal('0')
    if refund < 0:
        raise ValidationError("مبلغ الاسترداد مينفعش يبقى بالسالب.")
    # Cash can only come back for what we already paid beyond the (new) bill.
    overpaid = max(Decimal(str(inv.paid_amount)) - (Decimal(str(inv.total_amount))
                   - Decimal(str(inv.returned_amount)) - vendor_value), Decimal('0'))
    if refund > min(vendor_value, overpaid):
        raise ValidationError(
            f"الاسترداد النقدي أكبر من المسموح ({min(vendor_value, overpaid)}) — "
            f"المورد بيرجّع كاش بس عن اللي اتدفع له زيادة بعد المرتجع.")
    if refund and refund_treasury is None:
        raise ValidationError("اختار الخزنة اللي الاسترداد هيدخلها.")

    # Vendor payable: down by the value returned, back up by any cash he refunds.
    Vendor.objects.filter(pk=inv.vendor_id).update(balance=F('balance') - vendor_value + refund)
    PurchaseInvoice.objects.filter(pk=inv.pk).update(
        returned_amount=F('returned_amount') + vendor_value, paid_amount=F('paid_amount') - refund)

    narration = f"مرتجع مشتريات للمورد {inv.vendor.name} — فاتورة #{inv.id}" + (f" — {note}" if note else "")
    AccountingService.post_journal(
        description=narration[:255],
        lines=[
            {'account': 'ap', 'debit': vendor_value, 'credit': 0,
             'description': f"تخفيض مستحقات المورد {inv.vendor.name}"},
            {'account': 'import_expense', 'debit': landed_value - vendor_value, 'credit': 0,
             'description': "مصاريف وصول على بضاعة اترجعت"},
            {'account': 'inventory', 'debit': 0, 'credit': landed_value,
             'description': "خروج بضاعة مرتجعة للمورد"},
        ],
        journal_type='adjustment',
        reference=f"PRET-{inv.pk}",
        source=inv,
        created_by=user if getattr(user, 'is_authenticated', False) else None,
    )

    if refund:
        treasury = Treasury.objects.select_for_update().get(pk=refund_treasury.pk)
        # Treasury balance (signal) + Dr Cash / Cr AP (post_payment).
        FinancialTransaction.objects.create(
            treasury=treasury, transaction_type='in', amount=refund,
            description=f"استرداد من المورد {inv.vendor.name} عن مرتجع فاتورة #{inv.id}"[:255],
            vendor=inv.vendor, purchase_invoice=inv,
        )

    logger.info("[PURCHASE RETURN] PO #%s → %s lines, vendor value %s, refund %s",
                inv.pk, len(done), vendor_value, refund)
    return {'lines': done, 'vendor_value': vendor_value, 'landed_value': landed_value,
            'refund': refund, 'credit_left': vendor_value - refund}
