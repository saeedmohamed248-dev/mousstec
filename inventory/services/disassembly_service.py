"""
🔩 Disassembly Service — محرك الفك التدريجي المتعدد المستويات.

المسؤوليات:
- تخطيط عملية فك (إنشاء الحدث + الأبناء + سطور النتائج).
- تنفيذ التوزيع بطريقة القيمة البيعية (Sales Value Method) بنزاهة مالية صفرية.
- دعم عمق لا نهائي: الابن الناتج ممكن يتفكّك بدوره كأب لحدث جديد.

القانون المالي الصارم (Zero-Leakage):
    تكلفة الأب المعدّلة = تكلفة الأب − إجمالي إيراد الخردة
    وزن الابن          = سعر بيعه التقديري ÷ إجمالي أسعار البيع التقديرية
    تكلفة الابن        = الوزن × تكلفة الأب المعدّلة
    فروق المليمات تُضاف لأغلى ابن حتى يتحقق:
        Σ(تكلفة الأبناء) + إيراد الخردة  ≡  تكلفة الأب  (بالمليم بالظبط)
"""
import logging
import uuid
from decimal import ROUND_HALF_UP, Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger('mouss_tec_core')

CENTS = Decimal('0.01')
ZERO = Decimal('0.00')


def _q(value):
    """تقريب لأقرب مليم (قرشين عشريين) بنمط بنكي ثابت."""
    return Decimal(str(value)).quantize(CENTS, rounding=ROUND_HALF_UP)


class DisassemblyService:
    """كل عمليات الفك المالية بتمرّ من هنا."""

    # ------------------------------------------------------------------
    # 1) التخطيط — إنشاء حدث فك + أبناءه (بدون تنفيذ مالي بعد)
    # ------------------------------------------------------------------
    @staticmethod
    @transaction.atomic
    def plan_disassembly(parent_item, children, total_scrap_revenue=ZERO,
                         created_by=None, notes='', date=None):
        """
        ينشئ DisassemblyEvent وأبناءه كمسودة.

        children: قائمة dicts، كل واحد:
            { 'sku': str, 'name': str, 'estimated_sales_price': Decimal,
              'product': Product|None (اختياري), 'branch': Branch|None (اختياري),
              'child_item': InventoryItem|None (لو العنصر موجود سلفاً) }
        بيرجّع الـ DisassemblyEvent (مسودة). التنفيذ المالي عبر execute_disassembly.
        """
        from ..models import DisassemblyEvent, DisassemblyResult, InventoryItem

        if parent_item.status != InventoryItem.STATUS_IN_STOCK:
            raise ValidationError(
                f"العنصر الأب '{parent_item.name}' حالته "
                f"'{parent_item.get_status_display()}' ولا يمكن فكّه.")
        if not children:
            raise ValidationError("لازم تحدد عنصر ابن واحد على الأقل.")

        scrap = _q(total_scrap_revenue or ZERO)
        if scrap < ZERO:
            raise ValidationError("إيراد الخردة لا يمكن أن يكون سالباً.")
        if scrap > _q(parent_item.cost):
            raise ValidationError(
                "إيراد الخردة أكبر من تكلفة الأب — راجع الأرقام "
                "(النزاهة المالية تمنع تكلفة أبناء سالبة).")

        event = DisassemblyEvent.objects.create(
            parent_item=parent_item,
            date=date or timezone.now(),
            total_scrap_revenue=scrap,
            created_by=created_by,
            notes=notes or '',
        )

        for spec in children:
            est = _q(spec.get('estimated_sales_price') or ZERO)
            child = spec.get('child_item')
            if child is None:
                child = InventoryItem.objects.create(
                    sku=spec['sku'],
                    name=spec['name'],
                    estimated_sales_price=est,
                    cost=ZERO,  # تتحسب وقت التنفيذ
                    status=InventoryItem.STATUS_IN_STOCK,
                    product=spec.get('product'),
                    branch=spec.get('branch') or getattr(parent_item, 'branch', None),
                )
            DisassemblyResult.objects.create(
                event=event, child_item=child, estimated_sales_price=est)

        return event

    # ------------------------------------------------------------------
    # 2) التنفيذ — توزيع التكلفة بطريقة القيمة البيعية + موازنة الدفتر
    # ------------------------------------------------------------------
    @staticmethod
    @transaction.atomic
    def execute_disassembly(event):
        """
        ينفّذ التوزيع المالي لحدث الفك بشكل ذرّي (atomic).

        بعد التنفيذ:
          • كل ابن ياخد تكلفته المخصّصة (allocated_cost) في InventoryItem.cost
          • الأب حالته تبقى 'disassembled'
          • Σ(تكلفة الأبناء) + إيراد الخردة == تكلفة الأب بالظبط
        قابل للاستدعاء بشكل تكراري: الابن ممكن يبقى أب لحدث تاني لاحقاً.
        """
        from ..models import DisassemblyEvent, DisassemblyResult, InventoryItem

        # قفل الصفوف المعنية لمنع تنفيذ مزدوج/سباق
        event = (DisassemblyEvent.objects
                 .select_for_update()
                 .select_related('parent_item')
                 .get(pk=event.pk))
        parent = InventoryItem.objects.select_for_update().get(pk=event.parent_item_id)

        if event.is_executed:
            raise ValidationError("حدث الفك ده اتنفّذ قبل كده.")
        if parent.status != InventoryItem.STATUS_IN_STOCK:
            raise ValidationError(
                f"العنصر الأب حالته '{parent.get_status_display()}' — لا يمكن تنفيذ الفك.")

        results = list(DisassemblyResult.objects.select_for_update()
                       .filter(event=event).select_related('child_item'))
        if not results:
            raise ValidationError("مفيش عناصر أبناء لتوزيع التكلفة عليها.")

        parent_cost = _q(parent.cost)
        scrap = _q(event.total_scrap_revenue or ZERO)
        adjusted = _q(parent_cost - scrap)  # تكلفة الأب المعدّلة

        total_est = _q(sum((_q(r.estimated_sales_price) for r in results), ZERO))
        if total_est <= ZERO:
            raise ValidationError(
                "إجمالي أسعار البيع التقديرية = صفر — طريقة القيمة البيعية "
                "محتاجة سعر تقديري موجب لواحد على الأقل من الأبناء.")

        # توزيع أولي بطريقة القيمة البيعية
        allocations = []
        running = ZERO
        for r in results:
            weight = _q(r.estimated_sales_price) / total_est
            alloc = _q(adjusted * weight)
            allocations.append([r, alloc])
            running += alloc

        # 🩹 موازنة الدفتر: أي فرق مليمات (± من التقريب) يتضاف لأغلى ابن
        residual = _q(adjusted - running)
        if residual != ZERO:
            richest = max(allocations, key=lambda pair: _q(pair[0].estimated_sales_price))
            richest[1] = _q(richest[1] + residual)

        # كتابة النتائج + تحديث الأبناء
        for r, alloc in allocations:
            r.allocated_cost = alloc
            r.save(update_fields=['allocated_cost'])

            child = r.child_item
            child.cost = alloc
            if child.status != InventoryItem.STATUS_IN_STOCK:
                child.status = InventoryItem.STATUS_IN_STOCK
            child.save(update_fields=['cost', 'status', 'updated_at'])

        # 🛡️ حارس النزاهة المالية — لازم المعادلة تتحقق بالمليم بالظبط
        total_alloc = _q(sum((alloc for _, alloc in allocations), ZERO))
        ledger = _q(total_alloc + scrap)
        if ledger != parent_cost:
            raise ValidationError(
                f"فشل حارس النزاهة المالية: مجموع الأبناء ({total_alloc}) "
                f"+ الخردة ({scrap}) = {ledger} ≠ تكلفة الأب ({parent_cost}).")

        # تجميد القيم + إقفال الأب
        event.parent_cost_snapshot = parent_cost
        event.adjusted_parent_cost = adjusted
        event.is_executed = True
        event.executed_at = timezone.now()
        event.save(update_fields=[
            'parent_cost_snapshot', 'adjusted_parent_cost', 'is_executed', 'executed_at'])

        parent.status = InventoryItem.STATUS_DISASSEMBLED
        parent.save(update_fields=['status', 'updated_at'])

        logger.info(
            "🔩 Disassembly #%s executed: parent=%s cost=%s scrap=%s → %d children (Σ=%s)",
            event.pk, parent.sku, parent_cost, scrap, len(allocations), total_alloc)

        return {
            'event_id': event.pk,
            'parent_cost': parent_cost,
            'scrap_revenue': scrap,
            'adjusted_parent_cost': adjusted,
            'total_allocated': total_alloc,
            'children': [
                {'sku': r.child_item.sku, 'name': r.child_item.name,
                 'estimated_sales_price': _q(r.estimated_sales_price),
                 'allocated_cost': alloc}
                for r, alloc in allocations
            ],
            'ledger_balanced': True,
        }

    # ------------------------------------------------------------------
    # 3) اختصار: تخطيط + تنفيذ في خطوة واحدة
    # ------------------------------------------------------------------
    @staticmethod
    def disassemble(parent_item, children, total_scrap_revenue=ZERO,
                    created_by=None, notes='', date=None):
        event = DisassemblyService.plan_disassembly(
            parent_item, children, total_scrap_revenue=total_scrap_revenue,
            created_by=created_by, notes=notes, date=date)
        report = DisassemblyService.execute_disassembly(event)
        return event, report

    # ------------------------------------------------------------------
    # 4) قوالب الفك (Reverse BOM) — تحميل قالب في مسودة حدث فك
    # ------------------------------------------------------------------
    @staticmethod
    def _unique_sku(base):
        """يضمن SKU فريد للابن: base، ولو موجود يضيف لاحقة قصيرة."""
        from ..models import InventoryItem
        base = (base or 'ITEM').strip().replace(' ', '-')[:100] or 'ITEM'
        candidate = base
        if not InventoryItem.objects.filter(sku=candidate).exists():
            return candidate
        for _ in range(20):
            candidate = f"{base}-{uuid.uuid4().hex[:6]}"
            if not InventoryItem.objects.filter(sku=candidate).exists():
                return candidate
        return f"{base}-{uuid.uuid4().hex}"  # احتمال شبه مستحيل للتصادم

    @staticmethod
    def template_to_children(template, parent_item):
        """يحوّل بنود القالب لـ children specs (بدون إنشاء أي شيء).

        تقدير سعر كل بند:
          • لو له سعر افتراضي > 0 → يُستخدم كما هو.
          • غير كده لو له نسبة وزن % > 0 → السعر = النسبة × قيمة الأب المرجعية
            (سعر بيع الأب التقديري، أو تكلفته لو مفيش سعر تقديري).
        """
        ref_value = _q(parent_item.estimated_sales_price or ZERO)
        if ref_value <= ZERO:
            ref_value = _q(parent_item.cost or ZERO)

        parent_sku = getattr(parent_item, 'sku', '') or 'PARENT'
        children = []
        for item in template.items.all():
            est = _q(item.default_estimated_sales_price or ZERO)
            weight = Decimal(str(item.weight_percentage or ZERO))
            if est <= ZERO and weight > ZERO:
                est = _q(ref_value * weight / Decimal('100'))

            base = item.sku_prefix or (
                item.product.part_number if item.product_id else item.part_name)
            children.append({
                'sku': DisassemblyService._unique_sku(f"{parent_sku}-{base}"),
                'name': item.product.name if item.product_id else item.part_name,
                'estimated_sales_price': est,
                'product': item.product,
                'branch': getattr(parent_item, 'branch', None),
            })
        return children

    @staticmethod
    @transaction.atomic
    def load_template(parent_item, template, created_by=None, notes='',
                      date=None, override_scrap_revenue=None):
        """
        يبني مسودة DisassemblyEvent من قالب فك جاهز (Reverse BOM).

        بيولّد عناصر أبناء وسطور نتائج تلقائياً من بنود القالب. المستخدم
        بعدها يقدر يشيل بند (يمسح DisassemblyResult) أو يعدّل السعر التقديري
        قبل ما ينفّذ execute_disassembly. مفيش أي توزيع مالي في المرحلة دي.
        """
        from ..models import DisassemblyTemplate

        if not template.items.exists():
            raise ValidationError("القالب مفيهوش أي بنود لتحميلها.")

        scrap = (override_scrap_revenue if override_scrap_revenue is not None
                 else template.default_scrap_revenue)
        children = DisassemblyService.template_to_children(template, parent_item)

        note_head = f"[قالب: {template.name}]"
        full_notes = f"{note_head}\n{notes}".strip() if notes else note_head

        return DisassemblyService.plan_disassembly(
            parent_item, children, total_scrap_revenue=scrap,
            created_by=created_by, notes=full_notes, date=date)
