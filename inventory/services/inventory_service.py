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

            # 🛡️ [FIX H13]: Prevent negative inventory on cancel
            to_inv.refresh_from_db()
            if to_inv.quantity < instance.quantity:
                raise ValueError(
                    f"لا يمكن إلغاء التحويل — الكمية المتاحة في الفرع المستقبل ({to_inv.quantity}) "
                    f"أقل من الكمية المُحوَّلة ({instance.quantity})."
                )
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
    # Cycle Count — mobile inventory adjustment with loss recording
    # ------------------------------------------------------------------
    @staticmethod
    def execute_cycle_count(product, branch, actual_qty):
        """
        Adjust inventory to match a physical count.
        If shortage detected, record financial loss in branch treasury.
        Returns (variance, new_qty).
        """
        from inventory.models import Inventory, Treasury, FinancialTransaction
        from decimal import Decimal

        with transaction.atomic():
            inv, _ = Inventory.objects.select_for_update().get_or_create(
                product=product, branch=branch, defaults={'quantity': 0}
            )
            diff = actual_qty - inv.quantity
            inv.quantity = actual_qty
            inv.save()

            # Record shortage loss in treasury
            if diff < 0:
                treasury = Treasury.objects.filter(branch=branch, is_active=True).first()
                if treasury:
                    loss_value = Decimal(str(abs(diff))) * Decimal(str(product.average_cost))
                    FinancialTransaction.objects.create(
                        treasury=treasury,
                        transaction_type='out',
                        amount=loss_value,
                        description=f"تسوية عجز جرد — {product.name} ({abs(diff)} وحدة)",
                    )

            logger.info(
                "[CYCLE COUNT] %s adjusted to %s (variance=%s) @ branch#%s",
                product.part_number, actual_qty, diff, branch.pk,
            )
        return diff, actual_qty

    # ------------------------------------------------------------------
    # Scrap Cost Distribution — allocate purchase cost to yield items
    # ------------------------------------------------------------------
    @staticmethod
    def distribute_scrap_cost(job):
        """
        Distribute total purchase cost of a scrap dismantling job
        across yield items proportionally by market value.
        Marks job as completed (triggering signal for inventory addition).
        Returns number of items processed.
        Raises ValidationError if no yields or zero market value.
        """
        from inventory.models import ScrapDismantlingJob
        from decimal import Decimal

        if job.is_completed:
            raise ValidationError("العملية مغلقة مسبقاً.")

        with transaction.atomic():
            yields = list(job.yields.select_related('product').all())
            if not yields:
                raise ValidationError("لا توجد مكونات مسجلة.")

            total_market_value = sum(
                Decimal(str(y.product.retail_price)) * y.quantity for y in yields
            )
            if total_market_value == 0:
                raise ValidationError("أسعار السوق للمكونات صفرية.")

            total_cost = Decimal(str(job.total_purchase_cost))
            for y in yields:
                item_value = Decimal(str(y.product.retail_price)) * y.quantity
                coefficient = item_value / total_market_value
                y.estimated_cost_allocation = total_cost * coefficient
                y.save()

            job.is_completed = True
            job.save()  # Signal execute_scrap_dismantling_yield adds to inventory

        logger.info(
            "[SCRAP] Distributed cost %s EGP across %s items for job#%s",
            total_cost, len(yields), job.pk,
        )
        return len(yields)

    # ------------------------------------------------------------------
    # Scrap Dismantling Yield — add yielded parts to inventory
    # ------------------------------------------------------------------
    @staticmethod
    def execute_scrap_yield(job):
        """
        When a ScrapDismantlingJob transitions to completed,
        add yield items to branch inventory and recalculate weighted average cost.
        Called from signal: post_save(ScrapDismantlingJob).
        """
        from inventory.models import Inventory, Product
        from decimal import Decimal

        with transaction.atomic():
            for yield_item in job.yields.all():
                product = yield_item.product

                if job.branch:
                    inv, _ = Inventory.objects.get_or_create(
                        product=product,
                        branch=job.branch,
                        defaults={'quantity': 0},
                    )
                    Inventory.objects.filter(pk=inv.pk).update(
                        quantity=F('quantity') + yield_item.quantity
                    )

                total_current_qty = product.total_inventory_qty
                old_value = (
                    Decimal(str(max(total_current_qty - yield_item.quantity, 0)))
                    * Decimal(str(product.average_cost))
                )
                new_value = (
                    Decimal(str(yield_item.quantity))
                    * Decimal(str(yield_item.estimated_cost_allocation))
                )

                if total_current_qty > 0:
                    Product.objects.filter(pk=product.pk).update(
                        average_cost=(old_value + new_value) / Decimal(str(total_current_qty)),
                        purchase_price=yield_item.estimated_cost_allocation,
                    )

        logger.info(
            "[SCRAP YIELD] Executed yield for job#%s (%s items)",
            job.pk, job.yields.count(),
        )

    # ------------------------------------------------------------------
    # B2B Product Sync — cross-schema marketplace update on Product save
    # ------------------------------------------------------------------
    @staticmethod
    def sync_product_to_b2b(product_instance, override_price=None):
        """
        Sync product data to GlobalB2BMarketplace in public schema.
        Publishes if product is active, b2b-published, and has stock.
        Removes listing otherwise.
        override_price: If provided, use this instead of product's own price.
        """
        if connection.schema_name == 'public':
            return

        current_tenant_schema = connection.schema_name

        # Read tenant-schema data BEFORE switching to public schema
        total_qty = product_instance.total_inventory_qty
        is_published = product_instance.is_b2b_published
        is_active = product_instance.is_active

        try:
            from django.apps import apps
            from django_tenants.utils import schema_context

            with schema_context('public'):
                GlobalB2BMarketplace = apps.get_model('clients', 'GlobalB2BMarketplace')
                Client = apps.get_model('clients', 'Client')

                tenant = Client.objects.filter(schema_name=current_tenant_schema).first()
                if not tenant:
                    return

                if is_published and is_active and total_qty > 0:
                    if override_price:
                        wholesale = override_price
                    elif product_instance.b2b_wholesale_price > 0:
                        wholesale = product_instance.b2b_wholesale_price
                    else:
                        wholesale = product_instance.retail_price

                    GlobalB2BMarketplace.objects.update_or_create(
                        tenant=tenant,
                        part_number=product_instance.part_number,
                        condition=product_instance.condition,
                        defaults={
                            'product_name': product_instance.name,
                            'brand': product_instance.brand,
                            'wholesale_price': wholesale,
                            'available_qty': total_qty,
                        },
                    )
                else:
                    GlobalB2BMarketplace.objects.filter(
                        tenant=tenant,
                        part_number=product_instance.part_number,
                        condition=product_instance.condition,
                    ).delete()
        except Exception as e:
            logger.error(
                "[B2B AGENT] Market sync failed for '%s': %s",
                product_instance.part_number, e,
            )

    # ------------------------------------------------------------------
    # B2B Listing Approval
    # ------------------------------------------------------------------
    @staticmethod
    def approve_b2b_listing(listing_request, approved_price, reviewer):
        """
        Approve a B2BListingRequest and sync to the public marketplace.
        """
        listing_request.status = 'approved'
        listing_request.approved_price = approved_price
        listing_request.reviewed_by = reviewer
        listing_request.reviewed_at = timezone.now()
        listing_request.save(update_fields=[
            'status', 'approved_price', 'reviewed_by', 'reviewed_at',
        ])

        InventoryService.sync_product_to_b2b(
            listing_request.product, override_price=approved_price,
        )

        listing_request.is_synced = True
        listing_request.save(update_fields=['is_synced'])

        logger.info(
            "[B2B APPROVAL] Listing #%s approved at %s EGP by %s",
            listing_request.pk, approved_price, reviewer,
        )

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
