"""
📦 Inventory Service — Owns all stock mutations.

Responsibilities:
- Stock transfer execution (with deadlock prevention)
- Stock transfer cancellation (reverse inventory)
- Inventory movement tracking
- Stock alert creation / resolution
- B2B marketplace sync dispatch
"""

import logging
from django.db import transaction, connection
from django.db.models import F
from django.core.exceptions import ValidationError
from django.utils import timezone
from celery import current_app

logger = logging.getLogger('mouss_tec_core')


class InventoryService:
    """All inventory / stock operations go through here."""

    # ------------------------------------------------------------------
    # Stock Transfer — execute with ordered locking (deadlock prevention)
    # ------------------------------------------------------------------
    @staticmethod
    def execute_transfer(stock_transfer):
        """
        Move stock from source branch to destination branch.
        Uses sorted branch_id locking to prevent deadlocks.
        """
        from inventory.models import Inventory

        instance = stock_transfer
        with transaction.atomic():
            branch_ids = sorted([instance.from_branch_id, instance.to_branch_id])

            Inventory.objects.get_or_create(
                product=instance.product,
                branch_id=instance.to_branch_id,
                defaults={'quantity': 0},
            )

            locked_invs = list(
                Inventory.objects.select_for_update()
                .filter(product=instance.product, branch_id__in=branch_ids)
                .order_by('branch_id')
            )

            from_inv = next(i for i in locked_invs if i.branch_id == instance.from_branch_id)
            to_inv = next(i for i in locked_invs if i.branch_id == instance.to_branch_id)

            if from_inv.quantity < instance.quantity:
                raise ValidationError(
                    f"رصيد فرع المصدر لا يكفي لإتمام النقل! الرصيد الحالي: {from_inv.quantity}"
                )

            from_inv.quantity = F('quantity') - instance.quantity
            to_inv.quantity = F('quantity') + instance.quantity
            from_inv.save()
            to_inv.save()

            logger.info(
                "[TRANSFER] Moved %sx %s from branch#%s to branch#%s",
                instance.quantity, instance.product.name,
                instance.from_branch_id, instance.to_branch_id,
            )

    @staticmethod
    def cancel_transfer(stock_transfer):
        """
        Reverse an in-transit transfer. Uses same ordered-lock strategy.
        """
        from inventory.models import Inventory

        instance = stock_transfer
        with transaction.atomic():
            branch_ids = sorted([instance.from_branch_id, instance.to_branch_id])

            Inventory.objects.get_or_create(
                product=instance.product,
                branch_id=instance.from_branch_id,
                defaults={'quantity': 0},
            )
            Inventory.objects.get_or_create(
                product=instance.product,
                branch_id=instance.to_branch_id,
                defaults={'quantity': 0},
            )

            locked_invs = list(
                Inventory.objects.select_for_update()
                .filter(product=instance.product, branch_id__in=branch_ids)
                .order_by('branch_id')
            )

            from_inv = next(i for i in locked_invs if i.branch_id == instance.from_branch_id)
            to_inv = next(i for i in locked_invs if i.branch_id == instance.to_branch_id)

            from_inv.quantity = F('quantity') + instance.quantity
            from_inv.save()
            to_inv.quantity = F('quantity') - instance.quantity
            to_inv.save()

            logger.info(
                "[TRANSFER CANCELLED] Reverted %sx %s back to branch#%s",
                instance.quantity, instance.product.name, instance.from_branch_id,
            )

    # ------------------------------------------------------------------
    # Inventory Movement Tracking
    # ------------------------------------------------------------------
    @staticmethod
    def track_movement(inventory_instance):
        """
        Record every quantity change as an InventoryMovement entry.
        Uses _audit_old_instance from pre_save for comparison.
        """
        from inventory.models import (
            InventoryMovement, PurchaseInvoice, SaleInvoice,
        )
        from inventory.services.audit_service import AuditService

        old = getattr(inventory_instance, '_audit_old_instance', None)
        if old is None:
            return  # New record — no movement yet

        try:
            inventory_instance.refresh_from_db()
            new_qty = inventory_instance.quantity
            old_qty = old.quantity

            if old_qty == new_qty:
                return

            qty_change = new_qty - old_qty
            reason = 'manual'
            ref_type = ''
            ref_id = None
            note = ''

            # Heuristic: attribute the movement to the most recent invoice
            if qty_change > 0:
                last_po = (
                    PurchaseInvoice.objects
                    .filter(branch=inventory_instance.branch, is_applied=True)
                    .order_by('-date_created')
                    .first()
                )
                if last_po and (timezone.now() - last_po.date_created).total_seconds() < 10:
                    reason = 'purchase'
                    ref_type = 'PurchaseInvoice'
                    ref_id = last_po.pk
                    note = f'فاتورة شراء #{last_po.pk}'
            elif qty_change < 0:
                last_sale = (
                    SaleInvoice.objects
                    .filter(branch=inventory_instance.branch, is_applied=True)
                    .order_by('-date_created')
                    .first()
                )
                if last_sale and (timezone.now() - last_sale.date_created).total_seconds() < 10:
                    reason = 'sale'
                    ref_type = 'SaleInvoice'
                    ref_id = last_sale.pk
                    note = f'فاتورة بيع #{last_sale.pk}'

            InventoryMovement.objects.create(
                product=inventory_instance.product,
                branch=inventory_instance.branch,
                reason=reason,
                quantity_change=qty_change,
                quantity_before=old_qty,
                quantity_after=new_qty,
                reference_type=ref_type,
                reference_id=ref_id,
                note=note,
                created_by=AuditService.get_request_user(),
            )
        except Exception as e:
            logger.error("[INV MOVEMENT] Failed for %s: %s", inventory_instance, e)

    # ------------------------------------------------------------------
    # Stock Alerts
    # ------------------------------------------------------------------
    @staticmethod
    def check_alerts(inventory_instance):
        """
        After every Inventory save, check if we need to raise or resolve alerts.
        """
        from inventory.models import StockAlert

        try:
            inventory_instance.refresh_from_db()
            current_qty = inventory_instance.quantity
            min_level = inventory_instance.product.min_stock_level

            if current_qty <= 0:
                alert_type = 'out_of_stock'
            elif current_qty <= min_level:
                alert_type = 'low_stock'
            else:
                # Stock recovered — resolve open alerts
                StockAlert.objects.filter(
                    product=inventory_instance.product,
                    branch=inventory_instance.branch,
                    is_resolved=False,
                ).update(is_resolved=True, resolved_at=timezone.now())
                return

            existing = StockAlert.objects.filter(
                product=inventory_instance.product,
                branch=inventory_instance.branch,
                is_resolved=False,
            ).first()

            if existing:
                existing.current_quantity = current_qty
                existing.alert_type = alert_type
                existing.save(update_fields=['current_quantity', 'alert_type'])
            else:
                StockAlert.objects.create(
                    product=inventory_instance.product,
                    branch=inventory_instance.branch,
                    alert_type=alert_type,
                    current_quantity=current_qty,
                    min_stock_level=min_level,
                )
                logger.warning(
                    "[STOCK ALERT] %s — %s @ %s: %s/%s",
                    alert_type, inventory_instance.product.name,
                    inventory_instance.branch.name, current_qty, min_level,
                )
        except Exception as e:
            logger.error("[STOCK ALERT] Failed for %s: %s", inventory_instance, e)

    # ------------------------------------------------------------------
    # B2B Marketplace Sync (Celery dispatch)
    # ------------------------------------------------------------------
    @staticmethod
    def dispatch_b2b_sync(inventory_instance):
        """Dispatch async Celery task to sync product to B2B marketplace."""
        current_schema = connection.schema_name
        if current_schema == 'public':
            return

        product_id = inventory_instance.product_id

        def _dispatch():
            try:
                current_app.send_task(
                    'clients.tasks.async_sync_b2b_marketplace_product',
                    args=[current_schema, product_id],
                )
                logger.info(
                    "[B2B ROUTER] Dispatched sync for product#%s under '%s'",
                    product_id, current_schema,
                )
            except Exception as e:
                logger.error("[B2B ROUTER] Celery fail: %s", e)

        transaction.on_commit(_dispatch)

    @staticmethod
    def dispatch_b2b_delete(inventory_instance):
        """Dispatch async Celery task to remove product from B2B marketplace."""
        current_schema = connection.schema_name
        if current_schema == 'public':
            return

        part_number = inventory_instance.product.part_number
        condition = inventory_instance.product.condition

        def _dispatch():
            try:
                current_app.send_task(
                    'clients.tasks.async_remove_b2b_marketplace_product',
                    args=[current_schema, part_number, condition],
                )
                logger.info(
                    "[B2B ROUTER] Dispatched deletion for P/N %s under '%s'",
                    part_number, current_schema,
                )
            except Exception as e:
                logger.error("[B2B ROUTER] Deletion fail: %s", e)

        transaction.on_commit(_dispatch)
