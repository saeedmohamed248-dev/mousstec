import logging
import threading
from collections import defaultdict
from django.db import transaction, connection
from django.db.models import F, Sum
from django.db.models.signals import post_save, post_delete, pre_save
from django.dispatch import receiver
from django.core.exceptions import ValidationError
from decimal import Decimal
from django.utils import timezone
from datetime import timedelta

from .models import (Inventory, PurchaseInvoice, PurchaseInvoiceItem, Vendor,
                     SaleInvoice, SaleInvoiceItem, StockTransfer, FinancialTransaction)

# تهيئة الرادار لتسجيل الحركات بدقة
logger = logging.getLogger('mousstec_signals')

# =====================================================================
# 🧮 1. تحديث إجماليات الفواتير فقط (مسودات)
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
# ♻️ 2. أتمتة استرداد تأمين التوالف المتقدم (Core Charge Auto-Refund)
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
# 🛒 3. الاعتماد الفعلي للمشتريات (ماليات + مخزن + Escrow)
# =====================================================================
@receiver(post_save, sender=PurchaseInvoice)
def execute_purchase_posting(sender, instance, **kwargs):
    if instance.status == 'posted' and not instance.is_applied:
        with transaction.atomic():
            logger.info(f"🚀 [PURCHASE EXECUTION] Starting execution for PO #{instance.id}")
            
            # 1. المعاملة المالية (بحماية المبالغ الصفرية)
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

            # 2. 🛡️ الابتكار: تجميع الأصناف لتفادي تكرار الصنف في نفس الفاتورة
            product_qty_map = defaultdict(int)
            product_cost_map = {}
            for item in instance.items.select_related('product').all():
                product_qty_map[item.product_id] += item.quantity
                product_cost_map[item.product_id] = item.cost_price

            # ترتيب القطع بالـ ID قبل الحفظ لمنع Deadlocks
            sorted_product_ids = sorted(product_qty_map.keys())
            
            from .models import Product
            products = Product.objects.filter(id__in=sorted_product_ids).order_by('id')
            
            for product in products:
                added_qty = product_qty_map[product.id]
                cost_price = product_cost_map[product.id]
                
                total_current_qty = product.inventory_set.aggregate(Sum('quantity'))['quantity__sum'] or 0
                
                old_value = Decimal(str(total_current_qty)) * Decimal(str(product.average_cost))
                new_value = Decimal(str(added_qty)) * Decimal(str(cost_price))
                new_total_qty = total_current_qty + added_qty
                
                if new_total_qty > 0:
                    product.average_cost = (old_value + new_value) / Decimal(str(new_total_qty))
                    product.purchase_price = cost_price 
                    product.save(update_fields=['average_cost', 'purchase_price'])
                
                inv, _ = Inventory.objects.select_for_update().get_or_create(product=product, branch=instance.branch, defaults={'quantity': 0})
                inv.quantity = F('quantity') + added_qty
                inv.save()
                
            # 3. تحرير أموال الضمان للمزادات
            if instance.is_b2b_secured and instance.bidding_ref:
                try:
                    from clients.models import BlindBiddingRequest
                    bid = BlindBiddingRequest.objects.get(request_id=instance.bidding_ref)
                    if bid.status != 'completed':
                        bid.status = 'completed'
                        bid.save()
                        logger.info(f"⚖️ [ESCROW RELEASE] B2B Bid {bid.request_id} fulfilled.")
                except Exception as e:
                    logger.error(f"🔴 [ESCROW ERROR] {e}")

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

            # 2. 🤖 الذكاء الاصطناعي للصيانة الوقائية
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

            # 3. 🛡️ تجميع الأصناف لتفادي التصادم في قاعدة البيانات (Concurrent-Safe Check)
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
                        raise ValidationError(f"الكمية المتاحة من {product.name} لا تكفي لإتمام البيع!")
                    inv.quantity = F('quantity') - qty_to_deduct 
                    
                inv.save()
                inv.refresh_from_db()
                
                # 🚀 ابتكار: محرك إعادة الطلب المكتفي ذاتياً (Auto-Procurement Engine)
                if inv.quantity <= product.min_stock_level and not getattr(instance, 'is_return', False):
                    logger.warning(f"⚠️ [LOW STOCK ALERT] Product {product.part_number} dropped to {inv.quantity}")
                    # توليد فاتورة مشتريات آلية (مسودة) لتنبيه أمين المخزن
                    default_vendor = Vendor.objects.first()
                    if default_vendor:
                        # فحص ما إذا كان هناك طلب شراء مسودة معلق لنفس المورد لتجنب التكرار
                        draft_po, created = PurchaseInvoice.objects.get_or_create(
                            vendor=default_vendor, branch=instance.branch, status='draft',
                            defaults={'date_created': timezone.now()}
                        )
                        # إضافة الصنف للمسودة
                        po_item, item_created = PurchaseInvoiceItem.objects.get_or_create(
                            invoice=draft_po, product=product,
                            defaults={'quantity': product.min_stock_level * 2, 'cost_price': product.average_cost or product.purchase_price}
                        )
                        if item_created:
                            logger.info(f"🛒 [AUTO-PROCUREMENT] Draft PO generated for {product.name}.")
                    
            SaleInvoice.objects.filter(pk=instance.pk).update(is_applied=True)
            logger.info(f"✅ [SALE SUCCESS] INV #{instance.id} executed safely.")


# =====================================================================
# 🚚 5. النقل بين الفروع (التطبيق الصحيح لمنع الاختناق - True Deadlock Prevention)
# =====================================================================
@receiver(pre_save, sender=StockTransfer)
def execute_stock_transfer(sender, instance, **kwargs):
    if instance.id:
        old_instance = StockTransfer.objects.get(id=instance.id)
        if old_instance.status == 'pending' and instance.status == 'completed':
            with transaction.atomic():
                # الاستعلام عن الفرعين في خطوة واحدة مرتبة بقاعدة البيانات لمنع التقاطع
                branch_ids = sorted([instance.from_branch_id, instance.to_branch_id])
                
                Inventory.objects.get_or_create(product=instance.product, branch_id=instance.to_branch_id, defaults={'quantity': 0})
                
                locked_invs = list(Inventory.objects.select_for_update().filter(
                    product=instance.product, 
                    branch_id__in=branch_ids
                ).order_by('branch_id'))
                
                from_inv = next(i for i in locked_invs if i.branch_id == instance.from_branch_id)
                to_inv = next(i for i in locked_invs if i.branch_id == instance.to_branch_id)

                if from_inv.quantity < instance.quantity:
                    raise ValidationError(f"رصيد فرع المصدر لا يكفي! الرصيد الحالي: {from_inv.quantity}")
                
                from_inv.quantity = F('quantity') - instance.quantity
                to_inv.quantity = F('quantity') + instance.quantity
                
                from_inv.save()
                to_inv.save()
                logger.info(f"📦 [TRANSFER SUCCESS] Moved {instance.quantity}x {instance.product.name} to {instance.to_branch}")


# =====================================================================
# 🌐 6. المزامنة اللحظية مع السوق المركزي السحابي (Thread-Bomb Protected)
# =====================================================================
@receiver(post_save, sender=Inventory)
def sync_to_global_b2b_marketplace(sender, instance, **kwargs):
    current_schema = connection.schema_name
    if current_schema == 'public': return
    
    product_id = instance.product_id

    def do_sync_in_background():
        def run_thread():
            from django.db import connection, close_old_connections
            from django.db.models import Sum
            from django_tenants.utils import schema_context
            try:
                close_old_connections()
                with schema_context(current_schema):
                    from clients.models import Client
                    from inventory.models import Product, Inventory
                    tenant = Client.objects.get(schema_name=current_schema)
                    
                    if tenant.business_type in ['parts_dealer', 'both'] and tenant.is_marketplace_active:
                        product = Product.objects.get(id=product_id)
                        total_qty = Inventory.objects.filter(product=product).aggregate(Sum('quantity'))['quantity__sum'] or 0
                        product_data = {
                            "name": product.name, "brand": product.brand, 
                            "price": product.retail_price, "is_active": product.is_active, 
                            "b2b": getattr(product, 'is_b2b_published', False),
                            "pn": product.part_number, "cond": product.condition
                        }
                    else: return 

                with schema_context('public'):
                    from clients.models import GlobalB2BMarketplace
                    if total_qty > 0 and product_data['is_active'] and product_data['b2b']:
                        GlobalB2BMarketplace.objects.update_or_create(
                            tenant=tenant, part_number=product_data['pn'], condition=product_data['cond'],
                            defaults={
                                'product_name': product_data['name'], 'brand': product_data['brand'],
                                'wholesale_price': product_data['price'], 'available_qty': total_qty
                            }
                        )
                    else:
                        GlobalB2BMarketplace.objects.filter(
                            tenant=tenant, part_number=product_data['pn'], condition=product_data['cond']
                        ).delete()
            except Exception as e:
                logger.error(f"🔴 [B2B ASYNC SYNC ERROR] {e}")
            finally:
                close_old_connections()
                
        # 🚀 حماية: في الإنتاج الفعلي، يجب استبدال هذا بـ Celery Task لمنع الـ Thread Explosion
        threading.Thread(target=run_thread, daemon=True).start()

    transaction.on_commit(do_sync_in_background)

@receiver(post_delete, sender=Inventory)
def remove_from_global_b2b_marketplace(sender, instance, **kwargs):
    current_schema = connection.schema_name
    def do_delete():
        def run_thread():
            from django.db import connection, close_old_connections
            from django_tenants.utils import schema_context
            try:
                close_old_connections()
                with schema_context('public'):
                    from clients.models import GlobalB2BMarketplace
                    GlobalB2BMarketplace.objects.filter(
                        tenant__schema_name=current_schema, 
                        part_number=instance.product.part_number
                    ).delete()
            except: pass
            finally: close_old_connections()
        threading.Thread(target=run_thread, daemon=True).start()
        
    transaction.on_commit(do_delete)