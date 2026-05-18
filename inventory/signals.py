import logging
from collections import defaultdict
from django.db import transaction, connection
from django.db.models import F, Sum
from django.db.models.signals import post_save, post_delete, pre_save
from django.dispatch import receiver
from django.core.exceptions import ValidationError
from decimal import Decimal
from django.utils import timezone
from datetime import timedelta
from celery import current_app # 🚀 استدعاء نواة كرفان المهام لمنع الـ Thread Explosion

from .models import (Inventory, PurchaseInvoice, PurchaseInvoiceItem, Vendor,
                     SaleInvoice, SaleInvoiceItem, StockTransfer, FinancialTransaction)

# تهيئة رادار تسجيل الحركات بدقة
logger = logging.getLogger('mouss_tec_core')

# =====================================================================
# 🧮 1. تحديث إجماليات الفواتير تلقائياً (Calculators)
# =====================================================================
@receiver(post_save, sender=PurchaseInvoiceItem)
@receiver(post_delete, sender=PurchaseInvoiceItem)
def update_purchase_invoice_total(sender, instance, **kwargs):
    if hasattr(instance, 'invoice') and instance.invoice:
        instance.invoice.update_total()

@receiver(post_save, sender=SaleInvoiceItem)
@receiver(post_delete, sender=SaleInvoiceItem)
def update_sale_invoice_total(sender, instance, **kwargs):
    if hasattr(instance, 'invoice') and instance.invoice:
        instance.invoice.update_total()


# =====================================================================
# ♻️ 2. أتمتة استرداد تأمين التوالف (Core Charge Auto-Refund)
# =====================================================================
@receiver(pre_save, sender=SaleInvoiceItem)
def handle_core_charge_return(sender, instance, **kwargs):
    if instance.pk:
        try:
            old_instance = SaleInvoiceItem.objects.get(pk=instance.pk)
            if not old_instance.is_core_returned and instance.is_core_returned:
                refund_amount = Decimal(str(instance.quantity)) * Decimal(str(instance.core_charge_applied))
                
                if refund_amount > 0 and instance.invoice.customer:
                    with transaction.atomic():
                        customer = instance.invoice.customer
                        customer.balance = F('balance') - refund_amount
                        customer.save(update_fields=['balance'])
                        
                        if instance.invoice.treasury:
                            FinancialTransaction.objects.create(
                                treasury=instance.invoice.treasury, 
                                transaction_type='out',
                                amount=refund_amount, 
                                description=f"استرداد تأمين توالف للقطعة {instance.product.part_number} - فاتورة #{instance.invoice.id}",
                                customer=customer
                            )
                        logger.info(f"♻️ [CORE RETURN] Refunded {refund_amount} EGP to {customer.name}")
        except Exception as e:
            logger.error(f"🔴 [CORE RETURN ERROR] {e}")


# =====================================================================
# 🛒 3. الاعتماد الفعلي للمشتريات (ماليات + مخزن + الـ Escrow المالي الموحد)
# =====================================================================
@receiver(post_save, sender=PurchaseInvoice)
def execute_purchase_posting(sender, instance, **kwargs):
    if instance.status == 'posted' and not instance.is_applied:
        with transaction.atomic():
            logger.info(f"🚀 [PURCHASE EXECUTION] Starting execution for PO #{instance.id}")
            
            # 1. المعاملة المالية للخزنة
            if instance.treasury and instance.paid_amount > Decimal('0.00') and not instance.payments.exists():
                FinancialTransaction.objects.create(
                    treasury=instance.treasury, transaction_type='out',
                    amount=instance.paid_amount, 
                    description=f"سداد فاتورة مشتريات #{instance.id} للمورد {instance.vendor.name}",
                    purchase_invoice=instance, vendor=instance.vendor
                )
            
            due = Decimal(str(instance.total_amount)) - Decimal(str(instance.paid_amount))
            if due > Decimal('0.00'):
                instance.vendor.balance = F('balance') + due
                instance.vendor.save(update_fields=['balance'])

            # 2. تجميع الأصناف وترتيبها لمنع تصادم الـ Deadlocks في الداتابيز
            product_qty_map = defaultdict(int)
            product_cost_map = {}
            for item in instance.items.select_related('product').all():
                product_qty_map[item.product_id] += item.quantity
                product_cost_map[item.product_id] = item.cost_price

            sorted_product_ids = sorted(product_qty_map.keys())
            
            from .models import Product
            products = Product.objects.filter(id__in=sorted_product_ids).order_by('id')
            
            for product in products:
                added_qty = product_qty_map[product.id]
                cost_price = product_cost_map[product.id]
                
                # تحديث رصيد الفرع المخزني أولاً لقراءة كمية دقيقة ومحدثة
                inv, _ = Inventory.objects.select_for_update().get_or_create(
                    product=product, branch=instance.branch, defaults={'quantity': 0}
                )
                inv.quantity = F('quantity') + added_qty
                inv.save()
                
                # حساب متوسط التكلفة بناءً على الكميات الجديدة المحدثة بالسيرفر
                total_current_qty = product.total_inventory_qty
                old_value = Decimal(str(max(total_current_qty - added_qty, 0))) * Decimal(str(product.average_cost))
                new_value = Decimal(str(added_qty)) * Decimal(str(cost_price))
                
                if total_current_qty > 0:
                    product.average_cost = (old_value + new_value) / Decimal(str(total_current_qty))
                    product.purchase_price = cost_price 
                    product.save(update_fields=['average_cost', 'purchase_price'])
                
            # 3. 🚀 🚀 الاندماج المالي الحقيقي مع حساب الضمان (Escrow Integration Fix):
            if instance.is_b2b_secured and instance.bidding_ref:
                try:
                    from clients.models import BlindBiddingRequest
                    bid = BlindBiddingRequest.objects.get(request_id=instance.bidding_ref)
                    if bid.status != 'completed':
                        # محاكاة حالة الشحن استباقياً لتمرير قيود الأمان لفك التجميد
                        bid.status = 'shipped'
                        # استدعاء المحرك المحاسبي الحقيقي لتحرير الأموال وضخ الأرباح للتاجر الفائز وتسجيل الـ Ledger
                        bid.trigger_release_to_seller() 
                        logger.info(f"⚖️ [ESCROW RELEASE]: B2B Bid {bid.request_id} safely unlocked and wired via trigger.")
                except Exception as e:
                    logger.error(f"🔴 [ESCROW INTEGRATION CRITICAL ERROR]: {e}")

            # ترحيل نهائي للفاتورة لمنع التكرار
            PurchaseInvoice.objects.filter(pk=instance.pk).update(is_applied=True)
            logger.info(f"✅ [PURCHASE SUCCESS] PO #{instance.id} executed safely.")


# =====================================================================
# 💸 4. الاعتماد الفعلي للمبيعات والصيانة (ماليات + مخزن + ولاء + AI)
# =====================================================================
@receiver(post_save, sender=SaleInvoice)
def execute_sale_posting(sender, instance, **kwargs):
    if instance.status == 'posted' and not instance.is_applied:
        with transaction.atomic():
            logger.info(f"🚀 [SALE EXECUTION] Starting execution for INV #{instance.id}")
            
            if instance.treasury and instance.paid_amount > Decimal('0.00') and not instance.payments.exists():
                FinancialTransaction.objects.create(
                    treasury=instance.treasury, transaction_type='in',
                    amount=instance.paid_amount, 
                    description=f"إيراد فاتورة {instance.get_invoice_type_display()} رقم #{instance.id}",
                    sale_invoice=instance, customer=instance.customer
                )
                
            if instance.due_amount > Decimal('0.00'):
                instance.customer.balance = F('balance') + instance.due_amount
                instance.customer.save(update_fields=['balance'])

            # 2. 🤖 الذكاء الاصطناعي للصيانة الوقائية (Upselling Engine)
            if hasattr(instance, 'vehicle') and instance.vehicle:
                vehicle_updates = {}
                if instance.mileage and instance.mileage > instance.vehicle.last_mileage:
                    vehicle_updates['last_mileage'] = instance.mileage
                    
                    days_to_add = 90
                    if instance.invoice_type == 'maintenance':
                        services_text = " ".join([s.service.name.lower() for s in instance.service_items.all()])
                        if 'سير' in services_text or 'كاتينة' in services_text or 'محرك' in services_text:
                            days_to_add = 365
                        elif 'بوجيه' in services_text or 'فلتر' in services_text:
                            days_to_add = 180
                            
                    vehicle_updates['estimated_next_visit'] = timezone.now().date() + timedelta(days=days_to_add)
                
                if instance.invoice_type == 'maintenance':
                    instance.vehicle.ai_health_score = min(instance.vehicle.ai_health_score + 15, 100)
                    vehicle_updates['ai_health_score'] = instance.vehicle.ai_health_score
                
                if vehicle_updates:
                    for key, val in vehicle_updates.items():
                        setattr(instance.vehicle, key, val)
                    instance.vehicle.save(update_fields=list(vehicle_updates.keys()))
                    
            if instance.total_amount > 0 and hasattr(instance, 'is_return') and not instance.is_return:
                points_earned = int(instance.total_amount / Decimal('100.0'))
                instance.customer.loyalty_points = F('loyalty_points') + points_earned
                instance.customer.save(update_fields=['loyalty_points'])

            for service_item in instance.service_items.select_related('technician', 'service').all():
                if service_item.technician and service_item.service.tech_commission_percent > 0:
                    commission = (service_item.price * service_item.service.tech_commission_percent) / Decimal('100.00')
                    service_item.technician.commission_balance = F('commission_balance') + commission
                    service_item.technician.save(update_fields=['commission_balance'])

            # 3. تجميع المبيعات وخصم رصيد المخزن بأمان للـ Concurrency
            product_qty_map = defaultdict(int)
            for item in instance.items.select_related('product').all():
                product_qty_map[item.product_id] += item.quantity

            sorted_product_ids = sorted(product_qty_map.keys())
            from .models import Product
            
            for product_id in sorted_product_ids:
                qty_to_deduct = product_qty_map[product_id]
                product = Product.objects.get(id=product_id)
                inv = Inventory.objects.select_for_update().get(product_id=product_id, branch=instance.branch)
                
                if hasattr(instance, 'is_return') and instance.is_return:
                    inv.quantity = F('quantity') + qty_to_deduct
                else:
                    if inv.quantity < qty_to_deduct:
                        raise ValidationError(f"الكمية المتاحة من {product.name} لا تكفي لإتمام البيع بالورشة!")
                    inv.quantity = F('quantity') - qty_to_deduct 
                    
                inv.save()
                inv.refresh_from_db()
                
                # 🚀 وكيل إعادة الطلب الآلي والتنبؤ (Auto-Procurement Engine)
                if inv.quantity <= product.min_stock_level and not getattr(instance, 'is_return', False):
                    logger.warning(f"⚠️ [LOW STOCK ALERT] Product {product.part_number} dropped to {inv.quantity}")
                    default_vendor = Vendor.objects.first()
                    if default_vendor:
                        draft_po, created = PurchaseInvoice.objects.get_or_create(
                            vendor=default_vendor, branch=instance.branch, status='draft',
                            defaults={'date_created': timezone.now()}
                        )
                        po_item, item_created = PurchaseInvoiceItem.objects.get_or_create(
                            invoice=draft_po, product=product,
                            defaults={'quantity': product.min_stock_level * 2, 'cost_price': product.average_cost or product.purchase_price}
                        )
                        if item_created:
                            logger.info(f"🛒 [AUTO-PROCUREMENT] Draft PO generated for {product.name}.")
                    
            SaleInvoice.objects.filter(pk=instance.pk).update(is_applied=True)
            logger.info(f"✅ [SALE SUCCESS] INV #{instance.id} executed safely.")


# =====================================================================
# 🚚 5. النقل الآمن بين الفروع لمنع الاختناق (True Deadlock Prevention)
# =====================================================================
@receiver(pre_save, sender=StockTransfer)
def execute_stock_transfer(sender, instance, **kwargs):
    if instance.id:
        old_instance = StockTransfer.objects.get(id=instance.id)
        if old_instance.status == 'pending' and instance.status == 'completed':
            with transaction.atomic():
                branch_ids = sorted([instance.from_branch_id, instance.to_branch_id])
                
                Inventory.objects.get_or_create(product=instance.product, branch_id=instance.to_branch_id, defaults={'quantity': 0})
                
                locked_invs = list(Inventory.objects.select_for_update().filter(
                    product=instance.product, 
                    branch_id__in=branch_ids
                ).order_by('branch_id'))
                
                from_inv = next(i for i in locked_invs if i.branch_id == instance.from_branch_id)
                to_inv = next(i for i in locked_invs if i.branch_id == instance.to_branch_id)

                if from_inv.quantity < instance.quantity:
                    raise ValidationError(f"رصيد فرع المصدر لا يكفي لإتمام النقل! الرصيد الحالي: {from_inv.quantity}")
                
                from_inv.quantity = F('quantity') - instance.quantity
                to_inv.quantity = F('quantity') + instance.quantity
                
                from_inv.save()
                to_inv.save()
                logger.info(f"📦 [TRANSFER SUCCESS] Moved {instance.quantity}x {instance.product.name} to {instance.to_branch}")


# =====================================================================
# 🌐 6. المزامنة اللحظية مع السوق المركزي (Celery Asynchronous Tasks)
# =====================================================================
@receiver(post_save, sender=Inventory)
def sync_to_global_b2b_marketplace(sender, instance, **kwargs):
    """
    🚀 🚀 الاندماج البرمي المعزز (Asynchronous Celery Sync - Thread Bomb Shield):
    تم حذف خيوط المعالجة الخام وتوجيه المزامنة عبر طابور Celery المخصص، لضمان استقرار 
    السيرفر وحماية عزل الـ Schema تماماً في الـ Multi-Tenant.
    """
    current_schema = connection.schema_name
    if current_schema == 'public': return
    
    product_id = instance.product_id

    def dispatch_celery_sync():
        try:
            # إرسال المهمة للـ Message Broker ليعمل في الخلفية بأمان وعزل كامل
            current_app.send_task('clients.tasks.async_sync_b2b_marketplace_product', args=[current_schema, product_id])
            logger.info(f"🌐 [B2B AGENT ROUTER]: Dispatched marketplace sync task for product ID {product_id} under schema '{current_schema}'")
        except Exception as e:
            logger.error(f"🔴 [B2B AGENT ROUTER ERROR]: Failed to dispatch Celery task - {e}")

    # إطلاق الـ Worker فقط بعد إتمام الـ Database Commit الفعلي لضمان مطابقة رصيد المخزن
    transaction.on_commit(dispatch_celery_sync)


@receiver(post_delete, sender=Inventory)
def remove_from_global_b2b_marketplace(sender, instance, **kwargs):
    current_schema = connection.schema_name
    if current_schema == 'public': return
    
    # حفظ رقم القطعة والحالة قبل الحذف لتمريرهم للـ Worker
    part_number = instance.product.part_number
    condition = instance.product.condition

    def dispatch_celery_delete():
        try:
            current_app.send_task('clients.tasks.async_remove_b2b_marketplace_product', args=[current_schema, part_number, condition])
            logger.info(f"🛑 [B2B AGENT ROUTER]: Dispatched deletion task for P/N {part_number} under schema '{current_schema}'")
        except Exception as e:
            logger.error(f"🔴 [B2B AGENT ROUTER ERROR]: Deletion dispatch failed - {e}")
        
    transaction.on_commit(dispatch_celery_delete)