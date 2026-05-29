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

                # Weighted average cost recalculation
                total_current_qty = product.total_inventory_qty
                old_value = (
                    Decimal(str(max(total_current_qty - added_qty, 0)))
                    * Decimal(str(product.average_cost))
                )
                new_value = Decimal(str(added_qty)) * Decimal(str(cost_price))

                if total_current_qty > 0:
                    product.average_cost = (old_value + new_value) / Decimal(str(total_current_qty))
                    product.purchase_price = cost_price
                    product.save(update_fields=['average_cost', 'purchase_price'])

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

            # --- 1. Treasury income ---
            if (instance.treasury
                    and instance.paid_amount > Decimal('0.00')
                    and not instance.payments.exists()):
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

            # --- 2. Customer due balance ---
            if instance.due_amount > Decimal('0.00'):
                instance.customer.balance = F('balance') + instance.due_amount
                instance.customer.save(update_fields=['balance'])

            # --- 3. Vehicle telemetry ---
            if hasattr(instance, 'vehicle') and instance.vehicle:
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

            # --- 4. Loyalty points ---
            if (instance.total_amount > 0
                    and hasattr(instance, 'is_return') and not instance.is_return):
                points_earned = int(instance.total_amount / Decimal('100.0'))
                instance.customer.loyalty_points = F('loyalty_points') + points_earned
                instance.customer.save(update_fields=['loyalty_points'])

            # --- 5. Technician commissions ---
            for service_item in instance.service_items.select_related('technician', 'service').all():
                if service_item.technician and service_item.service.tech_commission_percent > 0:
                    base_commission = (
                        service_item.price * service_item.service.tech_commission_percent
                    ) / Decimal('100.00')

                    # Time-saving performance bonus (+10%)
                    if (service_item.actual_hours > 0
                            and service_item.service.estimated_hours > 0
                            and service_item.actual_hours < service_item.service.estimated_hours):
                        base_commission *= Decimal('1.10')

                    service_item.technician.commission_balance = (
                        F('commission_balance') + base_commission
                    )
                    service_item.technician.save(update_fields=['commission_balance'])

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
