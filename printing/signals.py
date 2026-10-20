"""
📡 Printing signals — keep the money numbers in sync with their sources.

* PrintOrder.paid_amount   ← linked PrintTransactions + credit allocations
* PrintTreasury.balance    ← transaction deletes (incl. bulk deletes)
* PrintOrder.total_amount  ← its jobs' prices
* PrintOrder.status        ← its jobs' production stages
* PrintMaterial.quantity   ← material usage / stock adjustment removed
* MaterialPurchase totals  ← its lines; paid_amount ← its payments
* PriceQuotation totals    ← its lines (on delete)
* PrintOrder.status        ← its jobs' stages / material use (forward only)
* General ledger           ← every order / payment / purchase / adjustment (printing/ledger.py)

Without this, an admin could mark an order as paid (set paid_amount directly)
without crediting any PrintTreasury, creating phantom revenue. We make the
PrintTransaction model the single source of truth: whenever an 'in' txn is
saved or deleted against an order, recompute the order's paid_amount from
the linked transactions.
"""
from django.core.exceptions import ValidationError
from django.db.models import F
from django.db.models.signals import post_delete, post_save, pre_delete, pre_save
from django.dispatch import receiver

from . import ledger
from .models import (
    MaterialPurchase, MaterialPurchaseLine, PrintCreditAllocation, PrintJob, PrintJobMaterial,
    PrintMaterial, PrintOrder, PrintStockAdjustment, PrintTransaction, PrintTreasury,
    QuotationLine, _shift_job_snapshot,
)


def _recompute_paid_amount(order_id):
    """
    🐛 [FIX]: كان بيستنى on_commit — فأي حاجة بتقرا المدفوع جوه نفس العملية
    (تسديد من الرصيد المقدّم، كشف الحساب، القيد) كانت بتشوف رقم قديم.
    """
    order = PrintOrder.objects.filter(pk=order_id).first() if order_id else None
    if order is not None:
        PrintOrder.objects.filter(pk=order_id).update(paid_amount=order.compute_paid())


def _recompute_purchase_paid(purchase_id):
    p = MaterialPurchase.objects.filter(pk=purchase_id).first() if purchase_id else None
    if p is not None:
        MaterialPurchase.objects.filter(pk=purchase_id).update(paid_amount=p.compute_paid())


# ── Treasury transactions ────────────────────────────────────────────
@receiver(pre_save, sender=PrintTransaction)
def remember_txn_links(sender, instance, **kwargs):
    instance._old_links = (PrintTransaction.objects.filter(pk=instance.pk)
                           .values('order_id', 'purchase_id', 'customer_id', 'supplier_id').first()
                           if instance.pk else None) or {}


@receiver(post_save, sender=PrintTransaction)
def sync_order_paid_on_txn_save(sender, instance, **kwargs):
    old = getattr(instance, '_old_links', {}) or {}
    for oid in {instance.order_id, old.get('order_id')} - {None}:
        _recompute_paid_amount(oid)
    for pid in {instance.purchase_id, old.get('purchase_id')} - {None}:
        _recompute_purchase_paid(pid)
    ledger.on_txn_changed(instance.pk, customer_id=old.get('customer_id'), supplier_id=old.get('supplier_id'))


@receiver(post_delete, sender=PrintTransaction)
def sync_order_paid_on_txn_delete(sender, instance, **kwargs):
    _recompute_paid_amount(instance.order_id)
    _recompute_purchase_paid(instance.purchase_id)
    ledger.on_txn_changed(instance.pk, customer_id=instance.customer_id, supplier_id=instance.supplier_id)


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


@receiver(post_save, sender=PrintTreasury)
def post_treasury_opening_balance(sender, instance, created, **kwargs):
    """الرصيد اللي الخزنة بتتعمل بيه = رصيد افتتاحي (مدين النقدية / دائن حقوق الملكية)."""
    if created and instance.balance:
        ledger._safe(ledger.post_treasury_opening, instance, instance.balance)


@receiver(post_delete, sender=PrintTreasury)
def unpost_treasury_opening(sender, instance, **kwargs):
    ledger._safe(ledger.unpost, ledger.ref_treasury_opening(instance.pk), 'حذف خزنة')


# ── Credit allocations (customer advance → order) ────────────────────
@receiver(post_save, sender=PrintCreditAllocation)
@receiver(post_delete, sender=PrintCreditAllocation)
def sync_order_paid_on_allocation(sender, instance, **kwargs):
    _recompute_paid_amount(instance.order_id)


# ── Orders ───────────────────────────────────────────────────────────
@receiver(pre_save, sender=PrintOrder)
def remember_order_customer(sender, instance, **kwargs):
    instance._old_customer_id = (PrintOrder.objects.filter(pk=instance.pk)
                                 .values_list('customer_id', flat=True).first() if instance.pk else None)


@receiver(post_save, sender=PrintOrder)
def post_order_to_ledger(sender, instance, **kwargs):
    ledger.on_order_changed(instance.pk)
    old_customer = getattr(instance, '_old_customer_id', None)
    if old_customer and old_customer != instance.customer_id:
        ledger._safe(ledger.sync_customer, old_customer)


@receiver(post_delete, sender=PrintOrder)
def unpost_deleted_order(sender, instance, **kwargs):
    ledger.on_order_changed(instance.pk, customer_id=instance.customer_id)


# ── Order total + production status follow its jobs ──────────────────
@receiver(post_save, sender=PrintJob)
@receiver(post_delete, sender=PrintJob)
def sync_order_total_from_jobs(sender, instance, **kwargs):
    order = PrintOrder.objects.filter(pk=instance.order_id).first()
    if order is not None:
        order.recalc_total_from_jobs()
        order.sync_status_from_jobs()
        ledger.on_order_changed(order.pk)


# ── Material usage → stock + order cost ──────────────────────────────
@receiver(post_save, sender=PrintJobMaterial)
def post_material_usage(sender, instance, created, **kwargs):
    order = PrintOrder.objects.filter(jobs__pk=instance.job_id).first()
    if order is None:
        return
    if created:
        order.sync_status_from_jobs()   # أول خامة تتصرف = الشغل بدأ
    ledger.on_order_changed(order.pk)


@receiver(post_delete, sender=PrintJobMaterial)
def return_stock_on_usage_delete(sender, instance, **kwargs):
    PrintMaterial.objects.filter(pk=instance.material_id).update(quantity=F('quantity') + instance.quantity)
    _shift_job_snapshot(instance.job_id, -instance.total_cost)
    order_id = PrintJob.objects.filter(pk=instance.job_id).values_list('order_id', flat=True).first()
    if order_id:
        ledger.on_order_changed(order_id)


@receiver(post_save, sender=PrintMaterial)
def post_material_opening_stock(sender, instance, created, **kwargs):
    """الكمية اللي الخامة بتتضاف بيها = مخزون افتتاحي بقيمته (بعد كده الكمية بتتحرك بمستندات بس)."""
    if created and instance.quantity and instance.cost_per_unit:
        ledger._safe(ledger.post_material_opening, instance, instance.quantity * instance.cost_per_unit)


@receiver(post_delete, sender=PrintMaterial)
def unpost_material_opening(sender, instance, **kwargs):
    ledger._safe(ledger.unpost, ledger.ref_material_opening(instance.pk), 'حذف خامة')


# ── Material purchases ───────────────────────────────────────────────
@receiver(pre_delete, sender=MaterialPurchase)
def refuse_deleting_received_purchase(sender, instance, **kwargs):
    if instance.status == 'received':
        raise ValidationError("فاتورة الشراء المستلمة مابتتمسحش — الغيها الأول (الخامات بترجع للمورد).")


@receiver(post_save, sender=MaterialPurchase)
@receiver(post_delete, sender=MaterialPurchase)
def post_purchase_to_ledger(sender, instance, **kwargs):
    ledger.on_purchase_changed(instance.pk, supplier_id=instance.supplier_id)


@receiver(post_delete, sender=MaterialPurchaseLine)
def recalc_purchase_on_line_delete(sender, instance, **kwargs):
    purchase = MaterialPurchase.objects.filter(pk=instance.purchase_id).first()
    if purchase is not None:
        purchase.recalc_total()


# ── Stock adjustments ────────────────────────────────────────────────
@receiver(post_save, sender=PrintStockAdjustment)
def post_adjustment_to_ledger(sender, instance, **kwargs):
    ledger.on_adjustment_changed(instance.pk)


@receiver(pre_delete, sender=PrintStockAdjustment)
def refuse_unaffordable_adjustment_delete(sender, instance, **kwargs):
    qty = PrintMaterial.objects.filter(pk=instance.material_id).values_list('quantity', flat=True).first()
    if qty is not None and qty - instance.quantity_change < 0:
        raise ValidationError(f"مينفعش تمسح التسوية: الكمية اللي زادت اتصرف منها (المتاح {qty}).")


@receiver(post_delete, sender=PrintStockAdjustment)
def reverse_adjustment_on_delete(sender, instance, **kwargs):
    PrintMaterial.objects.filter(pk=instance.material_id).update(
        quantity=F('quantity') - instance.quantity_change)
    ledger.on_adjustment_changed(instance.pk)


# ── Quotation totals follow their lines ──────────────────────────────
@receiver(post_delete, sender=QuotationLine)
def recalc_quote_on_line_delete(sender, instance, **kwargs):
    """🐛 [FIX]: حذف بند من عرض السعر كان بيسيب الإجمالي القديم."""
    from .models import PriceQuotation
    quote = PriceQuotation.objects.filter(pk=instance.quotation_id).first()
    if quote is not None:
        quote.recalc_totals()
