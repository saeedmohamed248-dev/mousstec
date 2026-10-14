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


# 🔌 قاطع بسيط: لو الـ broker (Redis) وقع، مانحاولش نبعت مزامنة الماركت بليس
#    مع كل صنف في نفس الفاتورة (كل محاولة كانت بتعلّق الطلب) — نستنى دقيقة.
_BROKER_DOWN_UNTIL = 0.0


def _broker_down():
    import time
    return time.monotonic() < _BROKER_DOWN_UNTIL


def _mark_broker_down(seconds=60):
    import time
    global _BROKER_DOWN_UNTIL
    _BROKER_DOWN_UNTIL = time.monotonic() + seconds


def set_branch_stock(product, branch, new_qty, *, unit_cost=None, user=None,
                     reference_type='StockSet', reference_id=None, note='', opening=None):
    """📦 يظبط رصيد صنف في فرع على ``new_qty`` بالطريقة الصح (أي تحميل/تعديل مباشر):

    * الفرق بيتسجّل حركة مخزون (تسوية).
    * الزيادة: متوسط التكلفة بيتحسب مرجّح بتكلفة الوحدة (``unit_cost`` لو موجودة،
      وإلا المتوسط الحالي/سعر الشراء)، والقيمة بتتقيّد على المخزون — مقابل
      «رصيد افتتاحي» لو ده أول مخزون للصنف (``opening``) أو «فروقات جرد».
    * النقص: بيخرج بمتوسط التكلفة مقابل «فروقات جرد».

    🐛 [FIX]: الرفع/التعديل المباشر كان بيكتب الكمية بس — من غير قيد (حساب المخزون
    في الدفتر يبعد عن قيمته) ومن غير ما متوسط التكلفة يتحسب. لازم يتنادى جوه
    transaction.atomic. بيرجّع الفرق (موجب/سالب/صفر).
    """
    from decimal import Decimal
    from inventory.models import Inventory, InventoryMovement, Product
    from inventory.services.accounting_service import AccountingService

    new_qty = max(int(new_qty), 0)
    row, _ = Inventory.objects.select_for_update().get_or_create(
        product=product, branch=branch, defaults={'quantity': 0})
    before = row.quantity
    delta = new_qty - before
    if delta == 0:
        return 0
    prod = Product.objects.select_for_update().get(pk=product.pk)
    avg = Decimal(str(prod.average_cost or 0)) or Decimal(str(prod.purchase_price or 0))
    on_hand = max(int(prod.valued_qty or 0), 0)
    if opening is None:
        opening = on_hand == 0 and not InventoryMovement.objects.filter(product=prod).exists()
    if delta > 0:
        unit = Decimal(str(unit_cost)) if unit_cost and Decimal(str(unit_cost)) > 0 else avg
        value = (Decimal(delta) * unit).quantize(Decimal('0.01'))
        prod.average_cost = ((Decimal(on_hand) * avg + Decimal(delta) * unit)
                             / Decimal(on_hand + delta)).quantize(Decimal('0.01'))
        prod.save(update_fields=['average_cost'])
    else:
        value = (Decimal(-delta) * avg).quantize(Decimal('0.01'))
    row.quantity = new_qty
    row.save(update_fields=['quantity'])
    InventoryMovement.objects.create(
        product=prod, branch=branch, reason='adjustment', quantity_change=delta,
        quantity_before=before, quantity_after=new_qty, reference_type=reference_type,
        reference_id=reference_id or prod.pk, note=(note or 'تعديل رصيد')[:255],
        created_by=user if getattr(user, 'is_authenticated', False) else None)
    if value > 0:
        desc = f"{note or 'تعديل رصيد'} — {prod.name} ({prod.part_number}) {delta:+d}"[:255]
        ref = f"{reference_type}-{prod.pk}"[:60]
        try:
            with transaction.atomic():
                if delta > 0 and opening:
                    AccountingService.post_opening_stock(amount=value, description=desc, reference=ref, branch=branch,
                                                         created_by=user if getattr(user, 'is_authenticated', False) else None)
                else:
                    lines = [{'account': 'inventory', 'debit': value, 'credit': 0},
                             {'account': 'inventory_variance', 'debit': 0, 'credit': value}]
                    if delta < 0:
                        lines = [{'account': 'inventory_variance', 'debit': value, 'credit': 0},
                                 {'account': 'inventory', 'debit': 0, 'credit': value}]
                    AccountingService.post_journal(description=desc, lines=lines, journal_type='adjustment',
                                                   reference=ref, branch=branch)
        except Exception as exc:  # noqa: BLE001 — الدفتر مايوقفش تعديل المخزون
            logger.error("[STOCK SET] ledger posting failed for #%s: %s", prod.pk, exc)
    return delta


def reaverage_after_restock(product_id, added_qty, added_value):
    """↩️ بضاعة رجعت المخزن بقيمة معروفة (مرتجع بيع / حذف فاتورة بيع): متوسط
    التكلفة يتحسب مرجّح بيها — زي الشراء بالظبط — عشان قيمة المخزن (الكمية ×
    المتوسط) تفضل = حساب المخزون في الدفتر اللي اترد له نفس القيمة.
    بيتنادى بعد ما الكمية اتضافت للفرع، جوه transaction."""
    from decimal import Decimal
    from inventory.models import Product
    added_qty = int(added_qty or 0)
    if added_qty <= 0:
        return
    prod = Product.objects.select_for_update().get(pk=product_id)
    total = max(int(prod.valued_qty or 0), 0)
    if total <= 0:
        return
    before = max(total - added_qty, 0)
    avg = Decimal(str(prod.average_cost or 0))
    new_avg = ((Decimal(before) * avg + Decimal(str(added_value or 0))) / Decimal(total)).quantize(Decimal('0.01'))
    if new_avg != avg:
        prod.average_cost = max(new_avg, Decimal('0'))
        prod.save(update_fields=['average_cost'])


class InventoryService:
    """All inventory / stock operations go through here."""

    # ------------------------------------------------------------------
    # Stock Transfer — execute with ordered locking (deadlock prevention)
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # 🚚 تحويل بين الفروع — مسار واحد واضح:
    #   قيد الانتظار → في الطريق (بيخرج من فرع المصدر)
    #                → تم الاستلام (بيدخل فرع الوصول)
    #   في الطريق/قيد الانتظار → ملغي (اللي خرج بيرجع للمصدر)
    #   قيد الانتظار → تم الاستلام على طول (خروج + دخول مرة واحدة)
    # 🐛 [FIX]: المخزون كان بيتحرّك بس في «قيد الانتظار ← في الطريق» وبيدخل فرع
    #    الوصول ساعتها (قبل الاستلام)؛ تحويل اتعمل «في الطريق» أو اتنقل لـ«تم
    #    الاستلام» على طول مكانش بيحرّك حاجة؛ وإلغاء تحويل اتعدّلت كميته وهو في
    #    الطريق كان بيرجّع كمية غير اللي خرجت. ومفيش حركة مخزون للتحويل خالص.
    # ------------------------------------------------------------------
    TRANSFER_STEPS = {
        ('pending', 'in_transit'): ('ship',),
        ('pending', 'completed'): ('ship', 'receive'),
        ('in_transit', 'completed'): ('receive',),
        ('in_transit', 'cancelled'): ('return',),
        ('pending', 'cancelled'): (),
    }

    @staticmethod
    def _transfer_move(transfer, branch_id, change, reason, note):
        from inventory.models import Inventory, InventoryMovement
        row, _ = Inventory.objects.select_for_update().get_or_create(
            product_id=transfer.product_id, branch_id=branch_id, defaults={'quantity': 0})
        before = row.quantity
        if before + change < 0:
            raise ValidationError(
                f"رصيد «{transfer.product.name}» في فرع المصدر ({before}) مايكفيش لتحويل "
                f"{abs(change)}.")
        row.quantity = before + change
        row.save(update_fields=['quantity'])
        InventoryMovement.objects.create(
            product_id=transfer.product_id, branch_id=branch_id, reason=reason,
            quantity_change=change, quantity_before=before, quantity_after=before + change,
            reference_type='StockTransfer', reference_id=transfer.pk, note=note[:255])

    @staticmethod
    def apply_transfer_transition(transfer, old_status, new_status):
        """يطبّق أثر تغيير حالة التحويل على المخزون (جوه transaction). بيرفع
        ValidationError لو الانتقال مش مسموح أو رصيد المصدر مش كفاية."""
        if old_status == new_status:
            return
        steps = InventoryService.TRANSFER_STEPS.get((old_status, new_status))
        if steps is None:
            raise ValidationError(
                f"مينفعش التحويل يتنقل من «{old_status}» لـ«{new_status}» "
                f"(التحويل المستلَم أو الملغي مابيتغيّرش).")
        if transfer.from_branch_id == transfer.to_branch_id:
            raise ValidationError("فرع المصدر وفرع الوصول لازم يبقوا مختلفين.")
        qty = int(transfer.quantity or 0)
        if qty <= 0:
            raise ValidationError("كمية التحويل لازم تبقى أكبر من صفر.")
        ref = f"تحويل #{transfer.pk}: {transfer.from_branch} ← {transfer.to_branch}"
        with transaction.atomic():
            # ترتيب ثابت للأقفال (رقم الفرع) يمنع الـ deadlock بين تحويلين عكس بعض
            for step in sorted(steps, key=lambda s: {'ship': 0, 'return': 0, 'receive': 1}[s]):
                if step == 'ship':
                    InventoryService._transfer_move(transfer, transfer.from_branch_id, -qty,
                                                    'transfer_out', f"{ref} — خرج من الفرع")
                elif step == 'receive':
                    InventoryService._transfer_move(transfer, transfer.to_branch_id, qty,
                                                    'transfer_in', f"{ref} — اتستلم")
                elif step == 'return':
                    InventoryService._transfer_move(transfer, transfer.from_branch_id, qty,
                                                    'transfer_in', f"{ref} — اتلغى ورجع للمصدر")
                InventoryService._transfer_journal(transfer, step, ref)
        logger.info("[TRANSFER] #%s %s → %s (%sx %s)", transfer.pk, old_status, new_status,
                    qty, transfer.product_id)

    @staticmethod
    def _transfer_journal(transfer, step, ref):
        """📒 قيد التحويل على مستوى الفرع (ميزان مراجعة كل فرع يشيل مخزونه الحقيقي):
          خروج:   فرع المصدر — مدين «بضاعة في الطريق» / دائن المخزون
          استلام: فرع الوصول — مدين المخزون / دائن «بضاعة في الطريق»
          إلغاء:  فرع المصدر — مدين المخزون / دائن «بضاعة في الطريق»
        الاستلام والإلغاء بنفس قيمة الخروج بالظبط، فالحساب الوسيط بيرجع صفر.

        🐛 [FIX]: التحويل كان بيحرّك الكميات بس — ميزان مراجعة فرع المصدر يفضل
        شايل بضاعة خرجت منه، وفرع الوصول بيبيعها فحساب مخزونه يطلع بالسالب."""
        from decimal import Decimal
        from django.db.models import Sum
        from inventory.models import AccountingEntry, Product
        from inventory.services.accounting_service import AccountingService
        out_ref = f"TRF-{transfer.pk}-OUT"
        if step == 'ship':
            prod = Product.objects.get(pk=transfer.product_id)
            unit = Decimal(str(prod.average_cost or 0)) or Decimal(str(prod.purchase_price or 0))
            value = (Decimal(int(transfer.quantity)) * unit).quantize(Decimal('0.01'))
        else:
            value = (AccountingEntry.objects
                     .filter(journal_entry__reference=out_ref,
                             account=AccountingService.account('inventory_in_transit'))
                     .exclude(journal_entry__status='reversed')
                     .aggregate(d=Sum('debit'))['d'] or Decimal('0'))
        if value <= 0:
            return
        into, branch, suffix, what = {
            'ship': ('inventory_in_transit', transfer.from_branch, 'OUT', 'خرج من الفرع'),
            'receive': ('inventory', transfer.to_branch, 'IN', 'اتستلم في الفرع'),
            'return': ('inventory', transfer.from_branch, 'BACK', 'اتلغى ورجع للمصدر'),
        }[step]
        out_of = 'inventory' if step == 'ship' else 'inventory_in_transit'
        try:
            with transaction.atomic():
                AccountingService.post_journal(
                    description=f"{ref} — {transfer.product.name} × {transfer.quantity} — {what}"[:255],
                    lines=[{'account': into, 'debit': value, 'credit': 0},
                           {'account': out_of, 'debit': 0, 'credit': value}],
                    journal_type='adjustment', reference=f"TRF-{transfer.pk}-{suffix}", branch=branch)
        except Exception as exc:  # noqa: BLE001 — الدفتر مايوقفش حركة المخزون
            logger.error("[TRANSFER] ledger posting failed for #%s (%s): %s", transfer.pk, step, exc)

    @staticmethod
    def in_transit_quantities():
        """{product_id: كمية في الطريق} — خرجت من فرع ولسه مادخلتش فرع (لسه أصل في الدفتر)."""
        from django.db.models import Sum
        from inventory.models import StockTransfer
        return dict(StockTransfer.objects.filter(status='in_transit')
                    .values_list('product_id').annotate(q=Sum('quantity')))

    # ------------------------------------------------------------------
    # Inventory Movement Tracking
    # ------------------------------------------------------------------
    @staticmethod
    def track_movement(inventory_instance):
        """
        Safety net: record a quantity change nobody explained.

        🐛 [FIX]: كل حركة مخزون كانت بتتسجّل مرتين — مرة هنا ومرة من الكود
        اللي عمل الحركة (البيع/الشراء/المرتجع…) — فكارت الصنف بيتضاعف،
        والاستلام والمرتجعات كانت بتطلع «تعديل يدوي» لأن التخمين بالوقت
        بيفشل. دلوقتي بنستنى لحد ما العملية تتقفل (on_commit): لو فيه حركة
        صريحة لنفس التغيير (نفس الصنف/الفرع/قبل/بعد) مابنكتبش حاجة، ولو
        مفيش (تعديل مباشر من الأدمن مثلاً) بنسجّلها «تعديل يدوي».
        """
        from django.db import transaction as _txn
        from inventory.services.audit_service import AuditService

        old = getattr(inventory_instance, '_audit_old_instance', None)
        if old is None:
            return  # New record — no movement yet

        try:
            inventory_instance.refresh_from_db()
        except Exception as e:
            logger.error("[INV MOVEMENT] refresh failed for %s: %s", inventory_instance, e)
            return
        old_qty, new_qty = old.quantity, inventory_instance.quantity
        if old_qty == new_qty:
            return
        product_id, branch_id = inventory_instance.product_id, inventory_instance.branch_id
        user = AuditService.get_request_user()

        def _record_if_unexplained():
            from datetime import timedelta
            from inventory.models import InventoryMovement
            try:
                explained = InventoryMovement.objects.filter(
                    product_id=product_id, branch_id=branch_id,
                    quantity_before=old_qty, quantity_after=new_qty,
                    created_at__gte=timezone.now() - timedelta(minutes=10),
                ).exists()
                if explained:
                    return
                InventoryMovement.objects.create(
                    product_id=product_id, branch_id=branch_id, reason='manual',
                    quantity_change=new_qty - old_qty,
                    quantity_before=old_qty, quantity_after=new_qty,
                    note='تعديل مباشر على الكمية',
                    created_by=user if getattr(user, 'is_authenticated', False) else None,
                )
            except Exception as e:
                logger.error("[INV MOVEMENT] Failed for product %s: %s", product_id, e)

        _txn.on_commit(_record_if_unexplained)

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
        Adjust inventory to match a physical count and book the variance
        (shrinkage / overage) against the inventory account.
        Returns (variance, new_qty).

        🐛 [FIX]: العجز كان بيتسجّل «سحب» من أول خزنة في الفرع بتكلفة البضاعة
        الناقصة — فرصيد الخزنة في السيستم بيقل من غير ما جنيه يخرج من الدرج
        (الدرج والسيستم يختلفوا)، والزيادة ماكانش ليها أي قيد. دلوقتي الفرق
        قيد مخزون مقابل «فروقات جرد» من غير أي حركة نقدية.
        """
        from inventory.models import Inventory, InventoryMovement
        from inventory.services.accounting_service import AccountingService

        with transaction.atomic():
            inv, _ = Inventory.objects.select_for_update().get_or_create(
                product=product, branch=branch, defaults={'quantity': 0}
            )
            before = inv.quantity
            diff = actual_qty - before
            inv.quantity = actual_qty
            inv.save()
            if diff:
                InventoryMovement.objects.create(
                    product=product, branch=branch, reason='adjustment',
                    quantity_change=diff, quantity_before=before, quantity_after=actual_qty,
                    reference_type='CycleCount', note=f"جرد فعلي — {product.name}",
                )
                try:
                    with transaction.atomic():
                        AccountingService.post_stock_adjustment(
                            product=product, quantity_change=diff, branch=branch,
                            reference=f"COUNT-{branch.pk}-{product.pk}",
                            description=f"فرق جرد — {product.name} ({diff:+d} وحدة)",
                        )
                except Exception as exc:  # noqa: BLE001 — the ledger never blocks a count
                    logger.error("[CYCLE COUNT] ledger posting failed: %s", exc)

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
                # 🐛 [FIX]: الحقل «التكلفة التقديرية للقطعة» — كان بيتخزّن فيه نصيب
                #    السطر كله، والإضافة للمخزن بتضربه في الكمية تاني.
                y.estimated_cost_allocation = (total_cost * coefficient
                                               / max(y.quantity, 1)).quantize(Decimal('0.01'))
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

        if not job.branch:
            logger.warning("[SCRAP YIELD] job#%s has no branch — nothing added", job.pk)
            return
        with transaction.atomic():
            for yield_item in job.yields.select_related('product').all():
                product = yield_item.product
                qty = int(yield_item.quantity or 0)
                if qty <= 0:
                    continue
                # تكلفة القطعة (distribute_scrap_cost بيحسبها للقطعة الواحدة)
                unit = Decimal(str(yield_item.estimated_cost_allocation or 0))
                row = (Inventory.objects.select_for_update()
                       .filter(product=product, branch=job.branch).first())
                set_branch_stock(product, job.branch, (row.quantity if row else 0) + qty,
                                 unit_cost=unit, reference_type='ScrapYield', reference_id=job.pk,
                                 note=f"قطع من تقطيع #{job.pk}", opening=True)
                Product.objects.filter(pk=product.pk).update(purchase_price=unit)

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
            if _broker_down():
                return
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
                _mark_broker_down()
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
            if _broker_down():
                return
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
                _mark_broker_down()
                logger.error("[B2B ROUTER] Deletion fail: %s", e)

        transaction.on_commit(_dispatch)
