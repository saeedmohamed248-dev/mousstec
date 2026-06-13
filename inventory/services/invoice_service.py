"""
🧾 Invoice Service — Owns the full lifecycle of Purchase & Sale invoices.

Responsibilities:
- Purchase invoice posting (vendor balance, inventory, average cost, escrow)
- Sale invoice posting (customer balance, stock deduction, loyalty, vehicle, commissions)
- Auto-reorder on low stock
- Agent health reporting
"""

import logging
from collections import defaultdict
from decimal import Decimal
from datetime import timedelta

from django.db import transaction, connection
from django.db.models import F
from django.core.exceptions import ValidationError
from django.utils import timezone

from erp_core.orchestrator import AgentHealthMonitor, AgentEventBus

logger = logging.getLogger('mouss_tec_core')


class InvoiceService:
    """All purchase and sale invoice execution logic lives here."""

    # ==================================================================
    # PURCHASE POSTING
    # ==================================================================
    @staticmethod
    def execute_purchase(purchase_invoice):
        """
        Full purchase posting pipeline:
        1. Create treasury payment if applicable
        2. Update vendor balance for remaining due
        3. Add inventory (with select_for_update)
        4. Recalculate weighted average cost
        5. Release B2B escrow if applicable
        6. Mark as applied + report to agent bus
        """
        from inventory.models import (
            FinancialTransaction, Inventory, Product, PurchaseInvoice,
        )

        instance = purchase_invoice
        with transaction.atomic():
            # 🛡️ Idempotency guard — prevents double-execution under concurrency
            fresh = PurchaseInvoice.objects.select_for_update().filter(pk=instance.pk).first()
            if not fresh or fresh.is_applied:
                logger.info("[PURCHASE] PO #%s already applied or missing — skipping", instance.id)
                return

            logger.info("[PURCHASE] Starting execution for PO #%s", instance.id)

            # --- 1. Treasury payment ---
            if (instance.treasury
                    and instance.paid_amount > Decimal('0.00')
                    and not instance.payments.exists()):
                FinancialTransaction.objects.create(
                    treasury=instance.treasury,
                    transaction_type='out',
                    amount=instance.paid_amount,
                    description=(
                        f"سداد فاتورة مشتريات #{instance.id} للمورد {instance.vendor.name}"
                    ),
                    purchase_invoice=instance,
                    vendor=instance.vendor,
                )

            # --- 2. Vendor balance (remaining due) ---
            due = Decimal(str(instance.total_amount)) - Decimal(str(instance.paid_amount))
            if due > Decimal('0.00'):
                instance.vendor.balance = F('balance') + due
                instance.vendor.save(update_fields=['balance'])

            # --- 3 & 4. Inventory + average cost ---
            product_qty_map = defaultdict(int)
            product_cost_map = {}
            for item in instance.items.select_related('product').all():
                product_qty_map[item.product_id] += item.quantity
                product_cost_map[item.product_id] = item.cost_price

            sorted_product_ids = sorted(product_qty_map.keys())
            products = Product.objects.filter(id__in=sorted_product_ids).order_by('id')

            for product in products:
                added_qty = product_qty_map[product.id]
                cost_price = product_cost_map[product.id]

                inv, _ = Inventory.objects.select_for_update().get_or_create(
                    product=product,
                    branch=instance.branch,
                    defaults={'quantity': 0},
                )
                inv.quantity = F('quantity') + added_qty
                inv.save()

                # 🛡️ [FIX C3]: Weighted avg cost — lock product row to prevent race condition
                from inventory.models import Product as _Prod
                locked_product = _Prod.objects.select_for_update().get(pk=product.pk)
                total_current_qty = locked_product.total_inventory_qty
                old_value = (
                    Decimal(str(max(total_current_qty - added_qty, 0)))
                    * Decimal(str(locked_product.average_cost))
                )
                new_value = Decimal(str(added_qty)) * Decimal(str(cost_price))

                if total_current_qty > 0:
                    locked_product.average_cost = (old_value + new_value) / Decimal(str(total_current_qty))
                    locked_product.purchase_price = cost_price
                    locked_product.save(update_fields=['average_cost', 'purchase_price'])

            # --- 5. B2B Escrow release ---
            if instance.is_b2b_secured and instance.bidding_ref:
                try:
                    from clients.models import BlindBiddingRequest
                    bid = BlindBiddingRequest.objects.get(request_id=instance.bidding_ref)
                    if bid.status != 'completed':
                        bid.status = 'shipped'
                        bid.trigger_release_to_seller()
                        logger.info("[ESCROW] B2B Bid %s safely unlocked.", bid.request_id)
                except Exception as e:
                    logger.error("[ESCROW] Integration error: %s", e)

            # --- 6. Finalize ---
            PurchaseInvoice.objects.filter(pk=instance.pk).update(is_applied=True)
            instance.is_applied = True  # Sync in-memory to prevent re-execution on re-save
            AgentHealthMonitor.heartbeat(
                'inbound_orchestrator',
                schema=connection.schema_name,
                metadata={'last_po_id': instance.pk},
            )
            AgentEventBus.set_agent_state(
                'inbound_orchestrator',
                schema=connection.schema_name,
                state={'last_po_id': instance.pk, 'status': 'completed'},
            )
            logger.info("[PURCHASE] PO #%s executed successfully.", instance.id)

    # ==================================================================
    # SALE POSTING
    # ==================================================================
    @staticmethod
    def execute_sale(sale_invoice):
        """
        Full sale posting pipeline:
        1. Create treasury income if applicable
        2. Update customer balance for remaining due
        3. Update vehicle telemetry (mileage, next visit, health)
        4. Award loyalty points
        5. Calculate technician commissions (with performance bonus)
        6. Deduct inventory (with select_for_update + validation)
        7. Auto-reorder on low stock
        8. Mark as applied + report to agent bus
        """
        from inventory.models import (
            FinancialTransaction, Inventory, Product, SaleInvoice,
            PurchaseInvoice, PurchaseInvoiceItem, Vendor,
        )

        instance = sale_invoice
        with transaction.atomic():
            # 🛡️ Idempotency guard — re-read with lock + bail if already applied
            # Prevents double-execution from concurrent signals / manual calls
            fresh = SaleInvoice.objects.select_for_update().filter(pk=instance.pk).first()
            if not fresh or fresh.is_applied:
                logger.info("[SALE] INV #%s already applied or missing — skipping", instance.id)
                return

            logger.info("[SALE] Starting execution for INV #%s", instance.id)

            # --- 1. Treasury ---
            is_return = getattr(instance, 'is_return', False)
            if (instance.treasury
                    and instance.paid_amount > Decimal('0.00')
                    and not instance.payments.exists()):
                if is_return:
                    # مرتجع: سحب المبلغ من الخزينة (رد للعميل)
                    FinancialTransaction.objects.create(
                        treasury=instance.treasury,
                        transaction_type='out',
                        amount=instance.paid_amount,
                        description=(
                            f"مرتجع فاتورة #{getattr(instance, 'original_invoice_id', '') or ''} — رد مبلغ #{instance.id}"
                        ),
                        sale_invoice=instance,
                        customer=instance.customer,
                    )
                else:
                    FinancialTransaction.objects.create(
                        treasury=instance.treasury,
                        transaction_type='in',
                        amount=instance.paid_amount,
                        description=(
                            f"إيراد فاتورة {instance.get_invoice_type_display()} رقم #{instance.id}"
                        ),
                        sale_invoice=instance,
                        customer=instance.customer,
                    )

            # --- 2. Customer balance ---
            if is_return:
                # 🛡️ المرتجع: نخفّض رصيد العميل بمقدار الجزء *الآجل* من الفاتورة
                # الأصلية فقط. الجزء النقدي اتـ refund للعميل عبر الخزينة (FT out)،
                # فلو خفّضنا الرصيد بكامل total_amount يبقى العميل علينا فلوس
                # وهمية. (مثلاً: بيع كاش 400 → رصيد العميل=0 → مرتجع → كانت
                # المعادلة القديمة تخلي الرصيد=-400.)
                credit_portion = (
                    Decimal(str(instance.total_amount)) - Decimal(str(instance.paid_amount))
                )
                if credit_portion > Decimal('0.00'):
                    instance.customer.balance = F('balance') - credit_portion
                    instance.customer.save(update_fields=['balance'])
            elif instance.due_amount > Decimal('0.00'):
                instance.customer.balance = F('balance') + instance.due_amount
                instance.customer.save(update_fields=['balance'])

            # --- 3. Vehicle telemetry (skip for returns) ---
            if not is_return and hasattr(instance, 'vehicle') and instance.vehicle:
                vehicle_updates = {}
                if instance.mileage and instance.mileage > instance.vehicle.last_mileage:
                    vehicle_updates['last_mileage'] = instance.mileage

                    days_to_add = 90
                    if instance.invoice_type == 'maintenance':
                        services_text = " ".join(
                            [s.service.name.lower() for s in instance.service_items.select_related('service').all()]
                        )
                        if any(kw in services_text for kw in ('سير', 'كاتينة', 'محرك')):
                            days_to_add = 365
                        elif any(kw in services_text for kw in ('بوجيه', 'فلتر')):
                            days_to_add = 180

                    vehicle_updates['estimated_next_visit'] = (
                        timezone.now().date() + timedelta(days=days_to_add)
                    )

                if instance.invoice_type == 'maintenance':
                    instance.vehicle.ai_health_score = min(
                        instance.vehicle.ai_health_score + 15, 100
                    )
                    vehicle_updates['ai_health_score'] = instance.vehicle.ai_health_score

                if vehicle_updates:
                    for key, val in vehicle_updates.items():
                        setattr(instance.vehicle, key, val)
                    instance.vehicle.save(update_fields=list(vehicle_updates.keys()))

            # --- 4. Loyalty points — only for billable revenue, not free/warranty work ---
            if (instance.total_amount > 0
                    and hasattr(instance, 'is_return') and not instance.is_return
                    and instance.items.filter(is_billable=True).exists()):
                points_earned = int(instance.total_amount / Decimal('100.0'))
                if points_earned > 0:
                    instance.customer.loyalty_points = F('loyalty_points') + points_earned
                    instance.customer.save(update_fields=['loyalty_points'])

            # --- 5. Technician commissions (skip for returns) ---
            if is_return:
                pass  # Returns do not award commissions
            else:
                pass  # Fall through to commission logic below
            for service_item in (instance.service_items.select_related('technician', 'service').all() if not is_return else []):
                if service_item.technician and service_item.service.tech_commission_percent > 0:
                    from decimal import ROUND_HALF_UP
                    base_commission = (
                        (service_item.price * service_item.service.tech_commission_percent)
                        / Decimal('100.00')
                    ).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

                    # Time-saving performance bonus (+10%)
                    if (service_item.actual_hours > 0
                            and service_item.service.estimated_hours > 0
                            and service_item.actual_hours < service_item.service.estimated_hours):
                        base_commission = (base_commission * Decimal('1.10')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

                    service_item.technician.commission_balance = (
                        F('commission_balance') + base_commission
                    )
                    service_item.technician.save(update_fields=['commission_balance'])

                    # Double-entry: Debit commission expense / Credit commission payable
                    try:
                        from inventory.services.treasury_service import TreasuryService
                        from inventory.models import ChartOfAccount, AccountingEntry
                        commission_expense = TreasuryService._get_or_create_account(
                            '5200', 'عمولات الفنيين', 'expense'
                        )
                        commission_payable = TreasuryService._get_or_create_account(
                            '2100', 'عمولات مستحقة للموظفين', 'liability'
                        )
                        ref = f"COMM-INV{instance.pk}-EMP{service_item.technician.pk}"
                        # Use individual create() so AccountingEntry.clean() runs on each entry.
                        # bulk_create() skips model validation entirely.
                        debit_entry = AccountingEntry(
                            reference=ref,
                            description=f"عمولة فني — {service_item.service.name} (فاتورة #{instance.pk})",
                            account=commission_expense,
                            debit=base_commission,
                            credit=Decimal('0'),
                            sale_invoice=instance,
                        )
                        debit_entry.clean()
                        debit_entry.save()
                        credit_entry = AccountingEntry(
                            reference=ref,
                            description=f"عمولة مستحقة لـ {service_item.technician}",
                            account=commission_payable,
                            debit=Decimal('0'),
                            credit=base_commission,
                            sale_invoice=instance,
                        )
                        credit_entry.clean()
                        credit_entry.save()
                        AccountingEntry.validate_balanced(ref)
                    except Exception as _ce:
                        import logging as _l
                        _l.getLogger('mouss_tec_core').warning("[COMMISSION] ledger entry failed: %s", _ce)

            # --- 6. Inventory deduction ---
            product_qty_map = defaultdict(int)
            for item in instance.items.select_related('product').all():
                product_qty_map[item.product_id] += item.quantity

            sorted_product_ids = sorted(product_qty_map.keys())

            for product_id in sorted_product_ids:
                qty_to_deduct = product_qty_map[product_id]
                product = Product.objects.get(id=product_id)

                inv, _ = Inventory.objects.select_for_update().get_or_create(
                    product_id=product_id,
                    branch=instance.branch,
                    defaults={'quantity': 0},
                )

                if hasattr(instance, 'is_return') and instance.is_return:
                    inv.quantity = F('quantity') + qty_to_deduct
                else:
                    if inv.quantity < qty_to_deduct:
                        raise ValidationError(
                            f"أمان النظام: الكمية المتاحة من {product.name} "
                            f"({inv.quantity}) لا تكفي. الإجراء مرفوض لحماية المخزون."
                        )
                    inv.quantity = F('quantity') - qty_to_deduct

                inv.save()
                inv.refresh_from_db()

                # --- 7. Auto-reorder on low stock ---
                if (inv.quantity <= product.min_stock_level
                        and not getattr(instance, 'is_return', False)):
                    logger.warning(
                        "[LOW STOCK] Product %s dropped to %s",
                        product.part_number, inv.quantity,
                    )
                    default_vendor = Vendor.objects.first()
                    if default_vendor:
                        draft_po, _ = PurchaseInvoice.objects.get_or_create(
                            vendor=default_vendor,
                            branch=instance.branch,
                            status='draft',
                            defaults={'date_created': timezone.now()},
                        )
                        PurchaseInvoiceItem.objects.get_or_create(
                            invoice=draft_po,
                            product=product,
                            defaults={
                                'quantity': product.min_stock_level * 2,
                                'cost_price': product.average_cost or product.purchase_price,
                            },
                        )

            # --- 8. Finalize ---
            SaleInvoice.objects.filter(pk=instance.pk).update(is_applied=True)
            instance.is_applied = True  # Sync in-memory to prevent re-execution on re-save
            AgentHealthMonitor.heartbeat(
                'outbound_orchestrator',
                schema=connection.schema_name,
                metadata={'last_inv_id': instance.pk},
            )
            AgentEventBus.set_agent_state(
                'outbound_orchestrator',
                schema=connection.schema_name,
                state={'last_inv_id': instance.pk, 'status': 'completed'},
            )
            logger.info("[SALE] INV #%s executed successfully.", instance.id)

    # ==================================================================
    # SALE RETURN
    # ==================================================================
    @staticmethod
    def create_return_invoice(original_invoice, return_items=None):
        """
        Create a return (مرتجع) invoice linked to the original.

        Args:
            original_invoice: The posted SaleInvoice to return.
            return_items: Optional list of dicts [{'item_id': int, 'quantity': int}].
                          If None, returns all items at full quantity.
        Returns:
            The new SaleInvoice (as draft/quotation) with is_return=True.
        Raises:
            ValidationError on invalid input.
        """
        from inventory.models import SaleInvoice, SaleInvoiceItem

        if original_invoice.is_return:
            raise ValidationError("لا يمكن عمل مرتجع لفاتورة مرتجع.")
        if original_invoice.status != 'posted':
            raise ValidationError("لا يمكن عمل مرتجع لفاتورة غير معتمدة.")

        with transaction.atomic():
            return_inv = SaleInvoice.objects.create(
                invoice_type=original_invoice.invoice_type,
                is_return=True,
                original_invoice=original_invoice,
                status='quotation',
                customer=original_invoice.customer,
                vehicle=original_invoice.vehicle,
                branch=original_invoice.branch,
                treasury=original_invoice.treasury,
                notes=f"مرتجع فاتورة #{original_invoice.id}",
            )

            if return_items:
                item_map = {r['item_id']: r['quantity'] for r in return_items}
            else:
                item_map = None

            for orig_item in original_invoice.items.select_related('product').all():
                qty = item_map.get(orig_item.pk, orig_item.quantity) if item_map else orig_item.quantity
                if qty <= 0:
                    continue
                if qty > orig_item.quantity:
                    raise ValidationError(
                        f"كمية المرتجع ({qty}) أكبر من الكمية الأصلية "
                        f"({orig_item.quantity}) للقطعة {orig_item.product.name}"
                    )
                SaleInvoiceItem.objects.create(
                    invoice=return_inv,
                    product=orig_item.product,
                    quantity=qty,
                    unit_price=orig_item.unit_price,
                    cost_at_sale=orig_item.cost_at_sale,
                )

            return_inv.update_total()
            # 🛡️ Cash refund is bounded by what the customer actually paid on the
            # original. If they bought on credit ("آجل") and never paid, the
            # return cancels the receivable — there is nothing to refund in cash.
            # If they paid 100 out of 400 then returned everything, we refund 100
            # in cash and write off the 300 receivable.
            cash_refund = min(
                Decimal(str(return_inv.total_amount)),
                Decimal(str(original_invoice.paid_amount)),
            )
            SaleInvoice.objects.filter(pk=return_inv.pk).update(
                paid_amount=cash_refund,
            )
            return_inv.refresh_from_db()

            logger.info(
                "[RETURN] Created return INV #%s for original INV #%s",
                return_inv.id, original_invoice.id,
            )
            return return_inv
