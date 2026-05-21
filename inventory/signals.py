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
from celery import current_app

from .models import (Inventory, PurchaseInvoice, PurchaseInvoiceItem, Vendor,
                     SaleInvoice, SaleInvoiceItem, StockTransfer, FinancialTransaction, EmployeeProfile,
                     AuditLog, ChartOfAccount, AccountingEntry, InventoryMovement, StockAlert,
                     Product, Customer, Treasury, Branch)
from erp_core.orchestrator import AgentHealthMonitor, AgentEventBus, dlq

logger = logging.getLogger('mouss_tec_core')

# =====================================================================
# 🧮 1. تحديث إجماليات الفواتير تلقائياً (Dynamic Calculators)
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
# ♻️ 2. أتمتة استرداد تأمين التوالف (Double-Entry Core Charge Auto-Refund)
# =====================================================================
@receiver(pre_save, sender=SaleInvoiceItem)
def handle_core_charge_return(sender, instance, **kwargs):
    """
    🚀 ابتكار: استرداد آمن ذو قيد مزدوج، يخصم من الخزينة ويضيف للعميل.
    """
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
                            treasury = Treasury.objects.select_for_update().get(pk=instance.invoice.treasury.pk)

                            if treasury.balance < refund_amount:
                                raise ValidationError(f"❌ خزينة {treasury.name} لا تحتوي على رصيد كافٍ لرد تأمين الكور.")

                            treasury.balance = F('balance') - refund_amount
                            treasury.save(update_fields=['balance'])
                            
                            FinancialTransaction.objects.create(
                                treasury=treasury, 
                                transaction_type='out',
                                amount=refund_amount, 
                                description=f"استرداد تأمين توالف لقطعة {instance.product.part_number} (الفاتورة #{instance.invoice.id})",
                                customer=customer
                            )
                        logger.info(f"♻️ [CORE RETURN] Balanced Double-Entry: Refunded {refund_amount} EGP to {customer.name}")
        except ValidationError as ve:
            raise ve # تمرير التحذير للمستخدم عبر لوحة التحكم
        except Exception as e:
            logger.error(f"🔴 [CORE RETURN CRITICAL ERROR] {e}")


# =====================================================================
# 🛒 3. الاعتماد الفعلي للمشتريات (ماليات + مخزن + الـ Escrow المالي الموحد)
# =====================================================================
@receiver(post_save, sender=PurchaseInvoice)
def execute_purchase_posting(sender, instance, **kwargs):
    if instance.status == 'posted' and not instance.is_applied:
        with transaction.atomic():
            logger.info(f"🚀 [PURCHASE EXECUTION] Starting execution for PO #{instance.id}")
            
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
                
                inv, _ = Inventory.objects.select_for_update().get_or_create(
                    product=product, branch=instance.branch, defaults={'quantity': 0}
                )
                inv.quantity = F('quantity') + added_qty
                inv.save()
                
                total_current_qty = product.total_inventory_qty
                old_value = Decimal(str(max(total_current_qty - added_qty, 0))) * Decimal(str(product.average_cost))
                new_value = Decimal(str(added_qty)) * Decimal(str(cost_price))
                
                if total_current_qty > 0:
                    product.average_cost = (old_value + new_value) / Decimal(str(total_current_qty))
                    product.purchase_price = cost_price 
                    product.save(update_fields=['average_cost', 'purchase_price'])
                
            if instance.is_b2b_secured and instance.bidding_ref:
                try:
                    from clients.models import BlindBiddingRequest
                    bid = BlindBiddingRequest.objects.get(request_id=instance.bidding_ref)
                    if bid.status != 'completed':
                        bid.status = 'shipped'
                        bid.trigger_release_to_seller() 
                        logger.info(f"⚖️ [ESCROW RELEASE]: B2B Bid {bid.request_id} safely unlocked.")
                except Exception as e:
                    logger.error(f"🔴 [ESCROW INTEGRATION CRITICAL ERROR]: {e}")

            PurchaseInvoice.objects.filter(pk=instance.pk).update(is_applied=True)
            AgentHealthMonitor.heartbeat('inbound_orchestrator', schema=connection.schema_name,
                                         metadata={'last_po_id': instance.pk})
            AgentEventBus.set_agent_state('inbound_orchestrator', schema=connection.schema_name,
                                           state={'last_po_id': instance.pk, 'status': 'completed'})


# =====================================================================
# 💸 4. الاعتماد الفعلي للمبيعات والصيانة (Performance Gamification & AI)
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

            # 🚀 ابتكار: مقيم كفاءة الفنيين ومضاعف العمولات الآلي (Gamification)
            for service_item in instance.service_items.select_related('technician', 'service').all():
                if service_item.technician and service_item.service.tech_commission_percent > 0:
                    base_commission = (service_item.price * service_item.service.tech_commission_percent) / Decimal('100.00')
                    
                    # مكافأة إنجاز العمل أسرع من الوقت التقديري (Time-Saving Bonus +10%)
                    if service_item.actual_hours > 0 and service_item.service.estimated_hours > 0:
                        if service_item.actual_hours < service_item.service.estimated_hours:
                            base_commission *= Decimal('1.10') # 10% بونص أداء سريع
                            
                    service_item.technician.commission_balance = F('commission_balance') + base_commission
                    service_item.technician.save(update_fields=['commission_balance'])

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
                
                # 🚀 وكيل إعادة الطلب الآلي والتنبؤ
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
                    
            SaleInvoice.objects.filter(pk=instance.pk).update(is_applied=True)
            AgentHealthMonitor.heartbeat('outbound_orchestrator', schema=connection.schema_name,
                                         metadata={'last_inv_id': instance.pk})
            AgentEventBus.set_agent_state('outbound_orchestrator', schema=connection.schema_name,
                                           state={'last_inv_id': instance.pk, 'status': 'completed'})
            logger.info(f"✅ [SALE SUCCESS] INV #{instance.id} executed safely.")


# =====================================================================
# 🚚 5. النقل الآمن بين الفروع (Intelligent Deadlock Prevention)
# =====================================================================
@receiver(pre_save, sender=StockTransfer)
def execute_stock_transfer(sender, instance, **kwargs):
    if instance.id:
        old_instance = StockTransfer.objects.get(id=instance.id)
        if old_instance.status == 'pending' and instance.status == 'in_transit':
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
        
        # 🚀 ابتكار: "الجرد العكسي" الذكي عند الإلغاء
        elif old_instance.status == 'in_transit' and instance.status == 'cancelled':
            with transaction.atomic():
                from_inv, _ = Inventory.objects.get_or_create(product=instance.product, branch_id=instance.from_branch_id, defaults={'quantity': 0})
                from_inv.quantity = F('quantity') + instance.quantity
                from_inv.save()
                
                # خصمها من الفرع الوهمي للوجهة
                to_inv = Inventory.objects.filter(product=instance.product, branch_id=instance.to_branch_id).first()
                if to_inv:
                    to_inv.quantity = F('quantity') - instance.quantity
                    to_inv.save()
                
                logger.info(f"📦 [TRANSFER CANCELLED] Safely reverted {instance.quantity}x {instance.product.name} to {instance.from_branch}")


# =====================================================================
# 🌐 6. المزامنة اللحظية مع السوق المركزي (Celery Asynchronous Sync)
# =====================================================================
@receiver(post_save, sender=Inventory)
def sync_to_global_b2b_marketplace(sender, instance, **kwargs):
    current_schema = connection.schema_name
    if current_schema == 'public': return
    
    product_id = instance.product_id

    def dispatch_celery_sync():
        try:
            current_app.send_task('clients.tasks.async_sync_b2b_marketplace_product', args=[current_schema, product_id])
            logger.info(f"🌐 [B2B ROUTER]: Dispatched sync for product ID {product_id} under '{current_schema}'")
        except Exception as e:
            logger.error(f"🔴 [B2B ROUTER ERROR]: Celery fail - {e}")

    transaction.on_commit(dispatch_celery_sync)


@receiver(post_delete, sender=Inventory)
def remove_from_global_b2b_marketplace(sender, instance, **kwargs):
    current_schema = connection.schema_name
    if current_schema == 'public': return
    
    part_number = instance.product.part_number
    condition = instance.product.condition

    def dispatch_celery_delete():
        try:
            current_app.send_task('clients.tasks.async_remove_b2b_marketplace_product', args=[current_schema, part_number, condition])
            logger.info(f"🛑 [B2B ROUTER]: Dispatched deletion for P/N {part_number} under '{current_schema}'")
        except Exception as e:
            logger.error(f"🔴 [B2B ROUTER ERROR]: Deletion fail - {e}")
        
    transaction.on_commit(dispatch_celery_delete)


# =====================================================================
# 📋 7. سجل المراجعة التلقائي (Automatic Audit Trail)
# =====================================================================
AUDITED_MODELS = [
    'Product', 'Customer', 'Vendor', 'SaleInvoice', 'PurchaseInvoice',
    'Inventory', 'StockTransfer', 'FinancialTransaction', 'Treasury',
    'ChartOfAccount', 'AccountingEntry',
]

def _get_request_ip():
    """استخراج IP من الـ thread-local المُعبأ بواسطة AuditIPMiddleware"""
    try:
        from erp_core.middleware import _audit_thread_local
        return getattr(_audit_thread_local, 'ip', None)
    except Exception:
        return None

def _get_request_user():
    """استخراج المستخدم من الـ thread-local"""
    try:
        from erp_core.middleware import _audit_thread_local
        return getattr(_audit_thread_local, 'user', None)
    except Exception:
        return None

def _build_changes_dict(old_instance, new_instance):
    """بناء dict بالتغييرات (قبل/بعد) بين النسخة القديمة والجديدة"""
    changes = {}
    if old_instance is None:
        return changes
    for field in new_instance._meta.fields:
        fname = field.name
        old_val = getattr(old_instance, fname, None)
        new_val = getattr(new_instance, fname, None)
        if str(old_val) != str(new_val):
            changes[fname] = {'old': str(old_val), 'new': str(new_val)}
    return changes


def audit_pre_save(sender, instance, **kwargs):
    """حفظ نسخة من السجل قبل التعديل للمقارنة"""
    if sender.__name__ not in AUDITED_MODELS:
        return
    if instance.pk:
        try:
            instance._audit_old_instance = sender.objects.get(pk=instance.pk)
        except sender.DoesNotExist:
            instance._audit_old_instance = None
    else:
        instance._audit_old_instance = None


def audit_post_save(sender, instance, created, **kwargs):
    """تسجيل عملية الإنشاء أو التعديل في سجل المراجعة"""
    if sender.__name__ not in AUDITED_MODELS:
        return
    try:
        action = 'create' if created else 'update'
        old_inst = getattr(instance, '_audit_old_instance', None)
        changes = {} if created else _build_changes_dict(old_inst, instance)

        # لا نسجل تعديل إذا لم يتغير شيء فعلياً
        if action == 'update' and not changes:
            return

        AuditLog.objects.create(
            user=_get_request_user(),
            action=action,
            model_name=sender.__name__,
            object_id=str(instance.pk),
            object_repr=str(instance)[:255],
            changes_json=changes,
            ip_address=_get_request_ip(),
        )
    except Exception as e:
        logger.error(f"[AUDIT TRAIL] Failed to log {sender.__name__} #{instance.pk}: {e}")


def audit_post_delete(sender, instance, **kwargs):
    """تسجيل عملية الحذف في سجل المراجعة"""
    if sender.__name__ not in AUDITED_MODELS:
        return
    try:
        # حفظ نسخة كاملة من البيانات المحذوفة
        snapshot = {}
        for field in instance._meta.fields:
            snapshot[field.name] = str(getattr(instance, field.name, ''))

        AuditLog.objects.create(
            user=_get_request_user(),
            action='delete',
            model_name=sender.__name__,
            object_id=str(instance.pk),
            object_repr=str(instance)[:255],
            changes_json={'deleted_snapshot': snapshot},
            ip_address=_get_request_ip(),
        )
    except Exception as e:
        logger.error(f"[AUDIT TRAIL] Failed to log deletion of {sender.__name__} #{instance.pk}: {e}")


# ربط الإشارات بالموديلات المُراقبة
for _model_name in AUDITED_MODELS:
    try:
        from . import models as _inv_models
        _model_cls = getattr(_inv_models, _model_name, None)
        if _model_cls:
            pre_save.connect(audit_pre_save, sender=_model_cls, dispatch_uid=f'audit_pre_{_model_name}')
            post_save.connect(audit_post_save, sender=_model_cls, dispatch_uid=f'audit_post_{_model_name}')
            post_delete.connect(audit_post_delete, sender=_model_cls, dispatch_uid=f'audit_del_{_model_name}')
    except Exception as e:
        logger.warning(f"[AUDIT] Could not connect signals for {_model_name}: {e}")


# =====================================================================
# 📊 8. القيود المحاسبية التلقائية (Auto Accounting Entries)
# =====================================================================

def _get_or_create_account(code, name, account_type):
    """جلب أو إنشاء حساب محاسبي بأمان"""
    account, _ = ChartOfAccount.objects.get_or_create(
        code=code,
        defaults={'name': name, 'account_type': account_type}
    )
    return account


@receiver(post_save, sender=FinancialTransaction)
def generate_accounting_entries_from_transaction(sender, instance, created, **kwargs):
    """
    توليد قيود محاسبية مزدوجة تلقائياً من كل حركة مالية.
    إيداع: مدين الخزينة / دائن الإيرادات أو حساب العميل
    سحب: مدين المصروفات / دائن الخزينة
    """
    if not created:
        return  # لا نُكرر القيود عند التعديل
    try:
        ref = f"FT-{instance.pk}"
        user = _get_request_user()

        if instance.transaction_type == 'in':
            # مدين: الخزينة (أصل)
            cash_account = _get_or_create_account('1001', 'الخزينة النقدية', 'asset')
            AccountingEntry.objects.create(
                reference=ref, description=instance.description or 'إيداع نقدي',
                account=cash_account, debit=instance.amount, credit=Decimal('0'),
                financial_transaction=instance,
                sale_invoice=instance.sale_invoice,
                created_by=user,
            )
            # دائن: إيرادات أو حساب عميل
            if instance.sale_invoice:
                revenue_account = _get_or_create_account('4001', 'إيرادات المبيعات', 'revenue')
            else:
                revenue_account = _get_or_create_account('4099', 'إيرادات أخرى', 'revenue')
            AccountingEntry.objects.create(
                reference=ref, description=instance.description or 'إيراد',
                account=revenue_account, debit=Decimal('0'), credit=instance.amount,
                financial_transaction=instance,
                sale_invoice=instance.sale_invoice,
                created_by=user,
            )
        else:  # out
            # مدين: مصروف أو حساب مورد
            if instance.purchase_invoice:
                expense_account = _get_or_create_account('5001', 'تكلفة المشتريات', 'expense')
            elif instance.category:
                expense_account = _get_or_create_account(
                    f'5{instance.category.pk:03d}',
                    f'مصروفات — {instance.category.name}',
                    'expense'
                )
            else:
                expense_account = _get_or_create_account('5099', 'مصروفات عمومية', 'expense')
            AccountingEntry.objects.create(
                reference=ref, description=instance.description or 'صرف نقدي',
                account=expense_account, debit=instance.amount, credit=Decimal('0'),
                financial_transaction=instance,
                purchase_invoice=instance.purchase_invoice,
                created_by=user,
            )
            # دائن: الخزينة
            cash_account = _get_or_create_account('1001', 'الخزينة النقدية', 'asset')
            AccountingEntry.objects.create(
                reference=ref, description=instance.description or 'سحب نقدي',
                account=cash_account, debit=Decimal('0'), credit=instance.amount,
                financial_transaction=instance,
                purchase_invoice=instance.purchase_invoice,
                created_by=user,
            )
    except Exception as e:
        logger.error(f"[ACCOUNTING] Failed to generate entries for FT #{instance.pk}: {e}")


# =====================================================================
# 📦 9. تتبع حركات المخزون التلقائي (Inventory Movement Tracker)
# =====================================================================

@receiver(post_save, sender=Inventory)
def track_inventory_movement(sender, instance, **kwargs):
    """
    تسجيل كل تغيير في كمية المخزون كحركة مخزنية موثقة.
    يستخدم _audit_old_instance من الـ pre_save للمقارنة.
    """
    old = getattr(instance, '_audit_old_instance', None)
    if old is None:
        return  # سجل جديد — لا حركة بعد

    try:
        # تحديث الكمية الفعلية بعد F() expression
        instance.refresh_from_db()
        new_qty = instance.quantity
        old_qty = old.quantity

        if old_qty == new_qty:
            return

        qty_change = new_qty - old_qty

        # تحديد السبب بناءً على السياق
        reason = 'manual'
        ref_type = ''
        ref_id = None
        note = ''

        # نحاول تخمين السبب من آخر عملية
        if qty_change > 0:
            # بحث عن آخر فاتورة شراء معتمدة
            last_po = PurchaseInvoice.objects.filter(
                branch=instance.branch, is_applied=True
            ).order_by('-date_created').first()
            if last_po and (timezone.now() - last_po.date_created).total_seconds() < 10:
                reason = 'purchase'
                ref_type = 'PurchaseInvoice'
                ref_id = last_po.pk
                note = f'فاتورة شراء #{last_po.pk}'
        elif qty_change < 0:
            last_sale = SaleInvoice.objects.filter(
                branch=instance.branch, is_applied=True
            ).order_by('-date_created').first()
            if last_sale and (timezone.now() - last_sale.date_created).total_seconds() < 10:
                reason = 'sale'
                ref_type = 'SaleInvoice'
                ref_id = last_sale.pk
                note = f'فاتورة بيع #{last_sale.pk}'

        InventoryMovement.objects.create(
            product=instance.product,
            branch=instance.branch,
            reason=reason,
            quantity_change=qty_change,
            quantity_before=old_qty,
            quantity_after=new_qty,
            reference_type=ref_type,
            reference_id=ref_id,
            note=note,
            created_by=_get_request_user(),
        )
    except Exception as e:
        logger.error(f"[INV MOVEMENT] Failed to track movement for {instance}: {e}")


# =====================================================================
# 🚨 10. تنبيهات نقص المخزون التلقائية (Smart Stock Alerts)
# =====================================================================

@receiver(post_save, sender=Inventory)
def check_stock_alerts(sender, instance, **kwargs):
    """
    فحص مستوى المخزون بعد كل تحديث وإنشاء تنبيه ذكي إذا لزم الأمر.
    """
    try:
        instance.refresh_from_db()
        current_qty = instance.quantity
        min_level = instance.product.min_stock_level

        if current_qty <= 0:
            alert_type = 'out_of_stock'
        elif current_qty <= min_level:
            alert_type = 'low_stock'
        else:
            # إذا تعافى المخزون، أغلق التنبيهات المفتوحة
            StockAlert.objects.filter(
                product=instance.product,
                branch=instance.branch,
                is_resolved=False,
            ).update(is_resolved=True, resolved_at=timezone.now())
            return

        # تجنب التكرار: لا ننشئ تنبيه جديد إذا كان هناك تنبيه مفتوح لنفس المنتج/الفرع
        existing = StockAlert.objects.filter(
            product=instance.product,
            branch=instance.branch,
            is_resolved=False,
        ).first()

        if existing:
            # تحديث الكمية فقط
            existing.current_quantity = current_qty
            existing.alert_type = alert_type
            existing.save(update_fields=['current_quantity', 'alert_type'])
        else:
            StockAlert.objects.create(
                product=instance.product,
                branch=instance.branch,
                alert_type=alert_type,
                current_quantity=current_qty,
                min_stock_level=min_level,
            )
            logger.warning(
                f"🚨 [STOCK ALERT] {alert_type} — {instance.product.name} "
                f"@ {instance.branch.name}: {current_qty}/{min_level}"
            )
    except Exception as e:
        logger.error(f"[STOCK ALERT] Failed for {instance}: {e}")