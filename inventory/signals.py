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
    SaleInvoiceServiceItem, CustomerFeedback,
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


@receiver(post_save, sender=SaleInvoice)
def create_customer_feedback_on_post(sender, instance, **kwargs):
    """Pillar 4 — when an invoice transitions to 'posted', mint a public UUID
    feedback record so the cashier can share the rating link with the customer."""
    if instance.status == 'posted':
        CustomerFeedback.objects.get_or_create(sale_invoice=instance)


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
    TreasuryService.reverse_balance_on_delete(instance)


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
# 🌐 14. B2B Product Sync → InventoryService
# =====================================================================
@receiver(post_save, sender=Product)
def sync_b2b_marketplace(sender, instance, **kwargs):
    if connection.schema_name == 'public':
        return
    if instance.is_b2b_published and instance.is_active:
        # إنشاء طلب نشر بدلاً من النشر المباشر — يحتاج موافقة الإدارة
        from inventory.models import B2BListingRequest
        from inventory.services.audit_service import AuditService
        existing_pending = B2BListingRequest.objects.filter(
            product=instance, status='pending',
        ).exists()
        if not existing_pending:
            B2BListingRequest.objects.create(
                product=instance,
                requested_price=(
                    instance.b2b_wholesale_price
                    if instance.b2b_wholesale_price > 0
                    else instance.retail_price
                ),
                requested_by=AuditService.get_request_user(),
            )
    elif not instance.is_b2b_published:
        # إلغاء النشر — يُزال فوراً بدون موافقة
        InventoryService.sync_product_to_b2b(instance)


# =====================================================================
# 🏎️ 15. Scrap Dismantling Yield → InventoryService
# =====================================================================
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
        InventoryService.execute_scrap_yield(instance)
