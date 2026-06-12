"""
📡 Printing signals — keep PrintOrder.paid_amount in sync with the ledger.

Without this, an admin could mark an order as paid (set paid_amount directly)
without crediting any PrintTreasury, creating phantom revenue. We make the
PrintTransaction model the single source of truth: whenever an 'in' txn is
saved or deleted against an order, recompute the order's paid_amount from
the linked transactions.
"""
from decimal import Decimal

from django.db import transaction
from django.db.models import Q, Sum
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from .models import PrintOrder, PrintTransaction


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
