"""
📡 Printing signals — keep the money numbers in sync with their sources.

* PrintOrder.paid_amount   ← linked PrintTransactions + credit allocations
* PrintTreasury.balance    ← transaction deletes (incl. bulk deletes)
* PrintOrder.total_amount  ← its jobs' prices
* PrintOrder.status        ← its jobs' production stages
* PrintMaterial.quantity   ← material usage / stock adjustment removed
* MaterialPurchase totals  ← its lines; paid_amount ← its payments
* PrintExpenseBill.paid    ← its payments
* PrintOrder.credited      ← its credit notes (مرتجعات بعد التسليم)
* Treasury transfers       ← both legs go with the transfer
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
from django.db.models import F, Q
from django.db.models.signals import post_delete, post_save, pre_delete, pre_save
from django.dispatch import receiver

from . import ledger
from .models import (
    MaterialPurchase, MaterialPurchaseLine, MaterialPurchaseReturn, PrintCheque, PrintCreditAllocation,
    PrintCreditNote, PrintCustomer, PrintExpenseBill, PrintFixedAsset, PrintJob, PrintJobMaterial,
    PrintMaterial, PrintOrder, PrintStockAdjustment, PrintSupplier, PrintSupplierAllocation,
    PrintTransaction, PrintTreasury, PrintTreasuryTransfer, PrintVatReturn, QuotationLine,
    _shift_job_snapshot,
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


def _recompute_bill_paid(bill_id):
    bill = PrintExpenseBill.objects.filter(pk=bill_id).first() if bill_id else None
    if bill is not None:
        PrintExpenseBill.objects.filter(pk=bill_id).update(paid_amount=bill.compute_paid())


# ── Treasury transactions ────────────────────────────────────────────
@receiver(pre_save, sender=PrintTransaction)
def remember_txn_links(sender, instance, **kwargs):
    instance._old_links = (PrintTransaction.objects.filter(pk=instance.pk)
                           .values('order_id', 'purchase_id', 'bill_id', 'customer_id', 'supplier_id').first()
                           if instance.pk else None) or {}


@receiver(post_save, sender=PrintTransaction)
def sync_order_paid_on_txn_save(sender, instance, **kwargs):
    old = getattr(instance, '_old_links', {}) or {}
    for oid in {instance.order_id, old.get('order_id')} - {None}:
        _recompute_paid_amount(oid)
    for pid in {instance.purchase_id, old.get('purchase_id')} - {None}:
        _recompute_purchase_paid(pid)
    for bid in {instance.bill_id, old.get('bill_id')} - {None}:
        _recompute_bill_paid(bid)
    ledger.on_txn_changed(instance.pk, customer_id=old.get('customer_id'), supplier_id=old.get('supplier_id'))


@receiver(pre_delete, sender=PrintTransaction)
def refuse_deleting_linked_leg(sender, instance, **kwargs):
    """
    رجل واحدة من تحويل لو اتمسحت الحساب الوسيط بيفضل مفتوح، وثمن أصل ثابت لو اتمسح
    لوحده الأصل بيفضل بيتهلك من غير تكلفة — المستند نفسه هو اللي يتمسح.
    """
    if PrintTreasuryTransfer.objects.filter(Q(out_txn_id=instance.pk) | Q(in_txn_id=instance.pk)).exists():
        raise ValidationError("الحركة دي جزء من تحويل بين الخزائن — امسح التحويل نفسه (الحركتين بيتمسحوا مع بعض).")
    if (instance.fixed_asset_id and not getattr(instance, '_deleting_with_asset', False)
            and PrintFixedAsset.objects.filter(pk=instance.fixed_asset_id).exists()):
        raise ValidationError("الحركة دي ثمن/بيع أصل ثابت — امسح الأصل نفسه من «الأصول الثابتة».")
    if (not getattr(instance, '_with_parent', False)
            and PrintTransaction.objects.filter(withholding_txn_id=instance.pk).exists()):
        raise ValidationError("ده خصم من المنبع تابع لدفعة — عدّل أو امسح الدفعة نفسها.")
    if (not getattr(instance, '_with_cheque', False)
            and PrintCheque.objects.filter(Q(txn_id=instance.pk) | Q(bounce_txn_id=instance.pk)).exists()):
        raise ValidationError("الحركة دي بتاعة شيك — اتعامل معاه من شاشة «الشيكات».")


@receiver(post_delete, sender=PrintTransaction)
def sync_order_paid_on_txn_delete(sender, instance, **kwargs):
    _recompute_paid_amount(instance.order_id)
    _recompute_purchase_paid(instance.purchase_id)
    _recompute_bill_paid(instance.bill_id)
    ledger.on_txn_changed(instance.pk, customer_id=instance.customer_id, supplier_id=instance.supplier_id)


@receiver(post_delete, sender=PrintTransaction)
def delete_withholding_companion(sender, instance, **kwargs):
    """مسح الدفعة بيمسح الخصم من المنبع التابع ليها."""
    companion = PrintTransaction.objects.filter(pk=instance.withholding_txn_id).first() \
        if instance.withholding_txn_id else None
    if companion is not None:
        companion._with_parent = True
        companion.delete()


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


@receiver(pre_save, sender=PrintTreasury)
def remember_treasury_kind(sender, instance, **kwargs):
    instance._old_kind = (PrintTreasury.objects.filter(pk=instance.pk).values_list('kind', flat=True).first()
                          if instance.pk else None)


@receiver(post_save, sender=PrintTreasury)
def post_treasury_opening_balance(sender, instance, created, **kwargs):
    """الرصيد اللي الخزنة بتتعمل بيه = رصيد افتتاحي (مدين حساب الخزنة / دائن حقوق الملكية)."""
    if created:
        if instance.balance:
            ledger._safe(ledger.post_treasury_opening, instance, instance.balance)
        return
    old_kind = getattr(instance, '_old_kind', None)
    if old_kind and old_kind != instance.kind:
        ledger._safe(ledger.resync_treasury, instance)     # خزنة ↔ بنك: الرصيد ينقل لحسابه الجديد
    else:
        ledger._safe(ledger.treasury_account, instance)    # اسم الحساب يتبع اسم الخزنة


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


# ── Credit notes (مرتجعات / خصم / إعدام) ───────────────────────────────
@receiver(post_save, sender=PrintCreditNote)
def post_credit_note(sender, instance, **kwargs):
    ledger.on_credit_note_changed(instance.pk, instance.order_id, instance.customer_id)


@receiver(post_delete, sender=PrintCreditNote)
def unpost_credit_note(sender, instance, **kwargs):
    """مسح الإشعار بيمسح حركة رد الفلوس بتاعته (الفلوس بترجع الخزنة)."""
    if instance.refund_txn_id:
        txn = PrintTransaction.objects.filter(pk=instance.refund_txn_id).first()
        if txn is not None:
            txn.delete()
    ledger.on_credit_note_changed(instance.pk, instance.order_id, instance.customer_id)


# ── Customer / supplier opening balances ────────────────────────────
@receiver(post_save, sender=PrintCustomer)
def post_customer_opening(sender, instance, **kwargs):
    ledger._safe(ledger.sync_customer_opening, instance.pk)
    ledger._safe(ledger.sync_customer, instance.pk)


@receiver(post_delete, sender=PrintCustomer)
def unpost_customer(sender, instance, **kwargs):
    ledger._safe(ledger.unpost, ledger.ref_customer_opening(instance.pk), 'حذف عميل')
    ledger._safe(ledger.unpost, ledger.ref_customer(instance.pk), 'حذف عميل')


@receiver(post_save, sender=PrintSupplier)
def post_supplier_opening(sender, instance, **kwargs):
    ledger._safe(ledger.sync_supplier_opening, instance.pk)
    ledger._safe(ledger.sync_supplier, instance.pk)


@receiver(post_delete, sender=PrintSupplier)
def unpost_supplier(sender, instance, **kwargs):
    ledger._safe(ledger.unpost, ledger.ref_supplier_opening(instance.pk), 'حذف مورد')
    ledger._safe(ledger.unpost, ledger.ref_supplier(instance.pk), 'حذف مورد')


# ── Supplier credit allocations (تسديد من رصيدنا عند المورد) ─────────
@receiver(post_save, sender=PrintSupplierAllocation)
@receiver(post_delete, sender=PrintSupplierAllocation)
def sync_doc_paid_on_supplier_allocation(sender, instance, **kwargs):
    _recompute_purchase_paid(instance.purchase_id)
    _recompute_bill_paid(instance.bill_id)


# ── Cheques ──────────────────────────────────────────────────────────
@receiver(pre_delete, sender=PrintTreasuryTransfer)
def refuse_deleting_cheque_settlement(sender, instance, **kwargs):
    if PrintCheque.objects.filter(settle_transfer_id=instance.pk).exists():
        raise ValidationError("التحويل ده صرف شيك — مابيتمسحش (لو الشيك رجع سجّله «مرتجع» من شاشة الشيكات).")


# ── Fixed assets ─────────────────────────────────────────────────────
@receiver(pre_save, sender=PrintFixedAsset)
def remember_asset_dates(sender, instance, **kwargs):
    old = (PrintFixedAsset.objects.filter(pk=instance.pk).first() if instance.pk else None)
    instance._old_start = old.start if old else None
    instance._old_supplier_id = old.supplier_id if old else None


@receiver(post_save, sender=PrintFixedAsset)
def post_fixed_asset(sender, instance, **kwargs):
    ledger.on_asset_changed(instance.pk, supplier_id=getattr(instance, '_old_supplier_id', None),
                            rebuild_from=getattr(instance, '_old_start', None))


@receiver(post_delete, sender=PrintFixedAsset)
def unpost_fixed_asset(sender, instance, **kwargs):
    ledger.on_asset_changed(instance.pk, supplier_id=instance.supplier_id, rebuild_from=instance.start)


# ── Expense bills ────────────────────────────────────────────────────
@receiver(post_save, sender=PrintExpenseBill)
@receiver(post_delete, sender=PrintExpenseBill)
def post_expense_bill(sender, instance, **kwargs):
    ledger.on_bill_changed(instance.pk, supplier_id=instance.supplier_id)


# ── Purchase returns ─────────────────────────────────────────────────
@receiver(pre_delete, sender=MaterialPurchaseReturn)
def refuse_deleting_posted_return(sender, instance, **kwargs):
    if instance.status == 'posted':
        raise ValidationError("المرتجع المترحّل مابيتمسحش — الخامات خرجت فعلاً للمورد.")


@receiver(post_save, sender=MaterialPurchaseReturn)
@receiver(post_delete, sender=MaterialPurchaseReturn)
def post_purchase_return(sender, instance, **kwargs):
    supplier_id = MaterialPurchase.objects.filter(pk=instance.purchase_id).values_list('supplier_id', flat=True).first()
    ledger.on_purchase_return_changed(instance.pk, supplier_id=supplier_id)


# ── Treasury transfers ───────────────────────────────────────────────
@receiver(post_delete, sender=PrintTreasuryTransfer)
def delete_transfer_legs(sender, instance, **kwargs):
    for txn in PrintTransaction.objects.filter(pk__in=[instance.out_txn_id, instance.in_txn_id]):
        txn.delete()


# ── VAT returns ──────────────────────────────────────────────────────
@receiver(post_save, sender=PrintVatReturn)
def post_vat_return(sender, instance, **kwargs):
    ledger._safe(ledger.sync_vat_return, instance)


@receiver(post_delete, sender=PrintVatReturn)
def unpost_vat_return(sender, instance, **kwargs):
    ledger._safe(ledger.unpost, ledger.ref_vat_return(instance.pk), 'حذف إقرار ضريبة')


# ── Quotation totals follow their lines ──────────────────────────────
@receiver(post_delete, sender=QuotationLine)
def recalc_quote_on_line_delete(sender, instance, **kwargs):
    """🐛 [FIX]: حذف بند من عرض السعر كان بيسيب الإجمالي القديم."""
    from .models import PriceQuotation
    quote = PriceQuotation.objects.filter(pk=instance.quotation_id).first()
    if quote is not None:
        quote.recalc_totals()
