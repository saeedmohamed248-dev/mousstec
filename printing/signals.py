"""
📡 Printing signals — keep the money numbers in sync with their sources.

* PrintOrder.paid_amount   ← linked PrintTransactions
* PrintTreasury.balance    ← transaction deletes (incl. bulk deletes)
* PrintOrder.total_amount  ← its jobs' prices
* PrintMaterial.quantity   ← material usage removed from a job
* PriceQuotation totals    ← its lines (on delete)

Without this, an admin could mark an order as paid (set paid_amount directly)
without crediting any PrintTreasury, creating phantom revenue. We make the
PrintTransaction model the single source of truth: whenever an 'in' txn is
saved or deleted against an order, recompute the order's paid_amount from
the linked transactions.
"""
from decimal import Decimal

from django.db import transaction
from django.db.models import F, Q, Sum
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from .models import (
    PrintJob, PrintJobMaterial, PrintMaterial, PrintOrder, PrintTransaction, PrintTreasury,
    QuotationLine, _shift_job_snapshot,
)


def _recompute_paid_amount(order_id):
    if not order_id:
        return
    agg = PrintTransaction.objects.filter(order_id=order_id).aggregate(
        ins=Sum('amount', filter=Q(transaction_type='in')),
        outs=Sum('amount', filter=Q(transaction_type='out')),
    )
    paid = (agg['ins'] or Decimal('0')) - (agg['outs'] or Decimal('0'))
    if paid < Decimal('0'):
        paid = Decimal('0')
    PrintOrder.objects.filter(pk=order_id).update(paid_amount=paid)


@receiver(post_save, sender=PrintTransaction)
def sync_order_paid_on_txn_save(sender, instance, **kwargs):
    if instance.order_id:
        transaction.on_commit(lambda oid=instance.order_id: _recompute_paid_amount(oid))


@receiver(post_delete, sender=PrintTransaction)
def sync_order_paid_on_txn_delete(sender, instance, **kwargs):
    if instance.order_id:
        transaction.on_commit(lambda oid=instance.order_id: _recompute_paid_amount(oid))


@receiver(post_delete, sender=PrintTransaction)
def reverse_treasury_on_txn_delete(sender, instance, **kwargs):
    """
    Deleting a transaction reverses its effect on the treasury balance.

    🐛 [FIX]: العكس كان في ``PrintTransaction.delete()`` بس — والحذف الجماعي
    (زرار "حذف المحدد" في الأدمن) بيتخطى الدالة دي، فرصيد الخزنة كان بيفضل
    شايل حركات اتمسحت. الـ signal بيشتغل في الحالتين.
    """
    signed = instance.amount if instance.transaction_type == 'in' else -instance.amount
    PrintTreasury.objects.filter(pk=instance.treasury_id).update(balance=F('balance') - signed)


# ── Order total follows its jobs ─────────────────────────────────────
@receiver(post_save, sender=PrintJob)
@receiver(post_delete, sender=PrintJob)
def sync_order_total_from_jobs(sender, instance, **kwargs):
    order = PrintOrder.objects.filter(pk=instance.order_id).first()
    if order is not None:
        order.recalc_total_from_jobs()


# ── Material usage removed → stock comes back ────────────────────────
@receiver(post_delete, sender=PrintJobMaterial)
def return_stock_on_usage_delete(sender, instance, **kwargs):
    PrintMaterial.objects.filter(pk=instance.material_id).update(quantity=F('quantity') + instance.quantity)
    _shift_job_snapshot(instance.job_id, -instance.total_cost)


# ── Quotation totals follow their lines ──────────────────────────────
@receiver(post_delete, sender=QuotationLine)
def recalc_quote_on_line_delete(sender, instance, **kwargs):
    """🐛 [FIX]: حذف بند من عرض السعر كان بيسيب الإجمالي القديم."""
    from .models import PriceQuotation
    quote = PriceQuotation.objects.filter(pk=instance.quotation_id).first()
    if quote is not None:
        quote.recalc_totals()
