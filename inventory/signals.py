"""
📡 Inventory Signals — Thin dispatchers to the Service Layer.

Each signal handler does ONLY:
1. Guard condition (should this fire?)
2. Delegate to the appropriate service method

All business logic lives in inventory/services/*.py
"""

import logging
from django.db import connection
from django.db.models.signals import post_save, post_delete, pre_save
from django.dispatch import receiver

from .models import (
    Inventory, PurchaseInvoice, PurchaseInvoiceItem,
    SaleInvoice, SaleInvoiceItem, StockTransfer,
    FinancialTransaction, Product, ScrapDismantlingJob,
    SaleInvoiceServiceItem,
)
from .services.invoice_service import InvoiceService
from .services.inventory_service import InventoryService
from .services.treasury_service import TreasuryService
from .services.audit_service import AuditService, AUDITED_MODELS

logger = logging.getLogger('mouss_tec_core')


# =====================================================================
# 🧮 1. Invoice Total Recalculation (unchanged — pure delegation)
# =====================================================================
@receiver(post_save, sender=PurchaseInvoiceItem)
@receiver(post_delete, sender=PurchaseInvoiceItem)
def update_purchase_invoice_total(sender, instance, **kwargs):
    if hasattr(instance, 'invoice') and instance.invoice:
        if not getattr(instance.invoice, '_skip_update_total', False):
            instance.invoice.update_total()


@receiver(post_save, sender=SaleInvoiceItem)
@receiver(post_delete, sender=SaleInvoiceItem)
def update_sale_invoice_total(sender, instance, **kwargs):
    if hasattr(instance, 'invoice') and instance.invoice:
        if not getattr(instance.invoice, '_skip_update_total', False):
            instance.invoice.update_total()


@receiver(post_save, sender=SaleInvoiceServiceItem)
@receiver(post_delete, sender=SaleInvoiceServiceItem)
def auto_update_sale_service_invoice_total(sender, instance, **kwargs):
    if hasattr(instance, 'invoice') and instance.invoice:
        instance.invoice.update_total()


# =====================================================================
# ♻️ 2. Core Charge Refund → TreasuryService
# =====================================================================
@receiver(pre_save, sender=SaleInvoiceItem)
def handle_core_charge_return(sender, instance, **kwargs):
    if instance.pk:
        try:
            old_instance = SaleInvoiceItem.objects.get(pk=instance.pk)
            if not old_instance.is_core_returned and instance.is_core_returned:
                TreasuryService.process_core_refund(instance)
        except SaleInvoiceItem.DoesNotExist:
            pass
        except Exception as e:
            if hasattr(e, 'message_dict') or isinstance(e, Exception) and 'رصيد' in str(e):
                raise  # Re-raise ValidationError to show in admin
            logger.error("[CORE RETURN] Error: %s", e)


# =====================================================================
# 🛒 3. Purchase Posting → InvoiceService
# =====================================================================
@receiver(post_save, sender=PurchaseInvoice)
def execute_purchase_posting(sender, instance, **kwargs):
    if instance.status == 'posted' and not instance.is_applied:
        InvoiceService.execute_purchase(instance)


# =====================================================================
# 💸 4. Sale Posting → InvoiceService
# =====================================================================
@receiver(post_save, sender=SaleInvoice)
def execute_sale_posting(sender, instance, **kwargs):
    if instance.status == 'posted' and not instance.is_applied:
        InvoiceService.execute_sale(instance)


# =====================================================================
# 🚚 5. Stock Transfer → InventoryService
# =====================================================================
@receiver(pre_save, sender=StockTransfer)
def execute_stock_transfer(sender, instance, **kwargs):
    if instance.id:
        try:
            old_instance = StockTransfer.objects.get(id=instance.id)
        except StockTransfer.DoesNotExist:
            return

        if old_instance.status == 'pending' and instance.status == 'in_transit':
            InventoryService.execute_transfer(instance)
        elif old_instance.status == 'in_transit' and instance.status == 'cancelled':
            InventoryService.cancel_transfer(instance)


# =====================================================================
# 🌐 6. B2B Marketplace Sync → InventoryService
# =====================================================================
@receiver(post_save, sender=Inventory)
def sync_to_global_b2b_marketplace(sender, instance, **kwargs):
    InventoryService.dispatch_b2b_sync(instance)


@receiver(post_delete, sender=Inventory)
def remove_from_global_b2b_marketplace(sender, instance, **kwargs):
    InventoryService.dispatch_b2b_delete(instance)


# =====================================================================
# 📋 7. Audit Trail → AuditService
# =====================================================================
def _audit_pre_save(sender, instance, **kwargs):
    AuditService.snapshot_before_save(sender, instance)


def _audit_post_save(sender, instance, created, **kwargs):
    AuditService.log_save(sender, instance, created)


def _audit_post_delete(sender, instance, **kwargs):
    AuditService.log_delete(sender, instance)


# Connect audit signals to all audited models
for _model_name in AUDITED_MODELS:
    try:
        from . import models as _inv_models
        _model_cls = getattr(_inv_models, _model_name, None)
        if _model_cls:
            pre_save.connect(_audit_pre_save, sender=_model_cls, dispatch_uid=f'audit_pre_{_model_name}')
            post_save.connect(_audit_post_save, sender=_model_cls, dispatch_uid=f'audit_post_{_model_name}')
            post_delete.connect(_audit_post_delete, sender=_model_cls, dispatch_uid=f'audit_del_{_model_name}')
    except Exception as e:
        logger.warning("[AUDIT] Could not connect signals for %s: %s", _model_name, e)


# =====================================================================
# 📊 8. Accounting Entries → TreasuryService
# =====================================================================
@receiver(post_save, sender=FinancialTransaction)
def generate_accounting_entries_from_transaction(sender, instance, created, **kwargs):
    if created:
        TreasuryService.generate_accounting_entries(instance)


# =====================================================================
# 📦 9. Inventory Movement Tracking → InventoryService
# =====================================================================
@receiver(post_save, sender=Inventory)
def track_inventory_movement(sender, instance, **kwargs):
    InventoryService.track_movement(instance)


# =====================================================================
# 🚨 10. Stock Alerts → InventoryService
# =====================================================================
@receiver(post_save, sender=Inventory)
def check_stock_alerts(sender, instance, **kwargs):
    InventoryService.check_alerts(instance)


# =====================================================================
# 💰 11. Treasury Balance Update → TreasuryService
# =====================================================================
@receiver(post_save, sender=FinancialTransaction)
def update_treasury_balance(sender, instance, created, **kwargs):
    if created:
        TreasuryService.update_balance(instance)


@receiver(post_delete, sender=FinancialTransaction)
def reverse_treasury_balance_on_delete(sender, instance, **kwargs):
    """
    عند حذف حركة مالية — عكس التأثير على الخزنة.
    إيداع محذوف → خصم من الرصيد | مصروف محذوف → إضافة للرصيد.
    """
    from inventory.models import Treasury
    amount = Decimal(str(instance.amount))
    try:
        with transaction.atomic():
            if instance.transaction_type == 'in':
                Treasury.objects.filter(pk=instance.treasury_id).update(
                    balance=F('balance') - amount
                )
            elif instance.transaction_type == 'out':
                Treasury.objects.filter(pk=instance.treasury_id).update(
                    balance=F('balance') + amount
                )
        logger.info(
            "[TREASURY] Reversed %s %s EGP on treasury #%s (deleted)",
            "debit" if instance.transaction_type == 'in' else "credit",
            amount, instance.treasury_id,
        )
    except Exception as e:
        logger.error("[TREASURY] Failed to reverse balance on delete: %s", e)


# =====================================================================
# 🏢 12. Employee Profile Auto-Creation (lightweight, stays in signals)
# =====================================================================
from django.contrib.auth.models import User
from .models import EmployeeProfile

@receiver(post_save, sender=User)
def create_employee_profile(sender, instance, created, **kwargs):
    if created and connection.schema_name != 'public':
        EmployeeProfile.objects.get_or_create(user=instance)


# =====================================================================
# 📈 13. Product Price History (lightweight, stays in signals)
# =====================================================================
from .models import ProductPriceHistory

@receiver(pre_save, sender=Product)
def track_product_price_changes(sender, instance, **kwargs):
    if instance.pk:
        try:
            old = Product.objects.get(pk=instance.pk)
            if old.retail_price != instance.retail_price or old.average_cost != instance.average_cost:
                ProductPriceHistory.objects.create(
                    product=instance,
                    old_retail=old.retail_price, new_retail=instance.retail_price,
                    old_cost=old.average_cost, new_cost=instance.average_cost,
                )
        except Product.DoesNotExist:
            pass


# =====================================================================
# 🌐 14. B2B Product Sync (on Product save, stays in signals — cross-schema)
# =====================================================================
from django_tenants.utils import schema_context

@receiver(post_save, sender=Product)
def sync_b2b_marketplace(sender, instance, **kwargs):
    if connection.schema_name == 'public':
        return

    current_tenant_schema = connection.schema_name

    try:
        from django.apps import apps
        with schema_context('public'):
            GlobalB2BMarketplace = apps.get_model('clients', 'GlobalB2BMarketplace')
            Client = apps.get_model('clients', 'Client')

            tenant = Client.objects.filter(schema_name=current_tenant_schema).first()
            if not tenant:
                return

            total_qty = instance.total_inventory_qty

            if instance.is_b2b_published and instance.is_active and total_qty > 0:
                GlobalB2BMarketplace.objects.update_or_create(
                    tenant=tenant,
                    part_number=instance.part_number,
                    condition=instance.condition,
                    defaults={
                        'product_name': instance.name,
                        'brand': instance.brand,
                        'wholesale_price': (
                            instance.b2b_wholesale_price
                            if instance.b2b_wholesale_price > 0
                            else instance.retail_price
                        ),
                        'available_qty': total_qty,
                    },
                )
            else:
                GlobalB2BMarketplace.objects.filter(
                    tenant=tenant,
                    part_number=instance.part_number,
                    condition=instance.condition,
                ).delete()
    except Exception as e:
        logger.error("[B2B AGENT] Market sync failed for '%s': %s", instance.part_number, e)


# =====================================================================
# 🏎️ 15. Scrap Dismantling Yield (stays in signals — specialized flow)
# =====================================================================
from django.db import transaction
from django.db.models import F
from decimal import Decimal

@receiver(pre_save, sender=ScrapDismantlingJob)
def _track_scrap_completion_change(sender, instance, **kwargs):
    if instance.pk:
        try:
            instance._was_completed = (
                ScrapDismantlingJob.objects
                .filter(pk=instance.pk)
                .values_list('is_completed', flat=True)
                .first()
            )
        except Exception:
            instance._was_completed = None
    else:
        instance._was_completed = False


@receiver(post_save, sender=ScrapDismantlingJob)
def execute_scrap_dismantling_yield(sender, instance, **kwargs):
    was_completed = getattr(instance, '_was_completed', None)
    if instance.is_completed and was_completed is False:
        with transaction.atomic():
            for yield_item in instance.yields.all():
                product = yield_item.product

                if instance.branch:
                    inv, _ = Inventory.objects.get_or_create(
                        product=product,
                        branch=instance.branch,
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
