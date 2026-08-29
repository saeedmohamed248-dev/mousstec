# 🔩 محرك الفك التدريجي المتعدد المستويات (Multi-Level Recursive Disassembly)
# =============================================================================
# أي عنصر في المخزون ممكن يبقى "أب" (Parent) ويتفكّك لعناصر "أبناء" (Children).
# والابن نفسه ممكن يبقى أب لعملية فك تانية → عمق لا نهائي:
#   نص كت → محرك كامل → رأس المحرك → كامة ...
#
# توزيع التكلفة: "طريقة القيمة البيعية" (Sales Value Method) — تكلفة الأب
# (بعد خصم إيراد الخردة) تتوزّع على الأبناء بنسبة سعر بيعهم التقديري.
#
# نزاهة مالية صفرية التسريب (Zero-Leakage):
#   Σ(تكلفة الأبناء) + إيراد الخردة  ≡  تكلفة الأب  (بالمليم، بدون أي فرق)
from decimal import Decimal

from django.core.validators import MinValueValidator
from django.db import models
from django.utils import timezone
from django.contrib.auth.models import User
from django.utils.translation import gettext_lazy as _

from .catalog import Product
from .organization import Branch

MONEY = dict(max_digits=14, decimal_places=2)
ZERO = Decimal('0.00')


class InventoryItem(models.Model):
    """عنصر مخزون عام: نص كت، محرك كامل، دينامو واحد... أي حاجة.

    نفس الموديل بيمثّل الأب والابن — وده اللي بيخلّي الفك متعدد المستويات
    ممكن بلا نهاية. الابن الناتج من عملية فك ممكن يتفكّك بدوره لاحقاً.
    """

    STATUS_IN_STOCK = 'in_stock'
    STATUS_DISASSEMBLED = 'disassembled'
    STATUS_SOLD = 'sold'
    STATUS_CHOICES = (
        (STATUS_IN_STOCK, _('في المخزن')),
        (STATUS_DISASSEMBLED, _('تم تفكيكه')),
        (STATUS_SOLD, _('تم بيعه')),
    )

    sku = models.CharField(
        max_length=120, unique=True,
        verbose_name=_("SKU / رقم الشاسيه (VIN)"),
    )
    name = models.CharField(max_length=200, verbose_name=_("اسم العنصر"))
    cost = models.DecimalField(
        **MONEY, default=ZERO, validators=[MinValueValidator(ZERO)],
        verbose_name=_("التكلفة"),
    )
    estimated_sales_price = models.DecimalField(
        **MONEY, default=ZERO, validators=[MinValueValidator(ZERO)],
        verbose_name=_("سعر البيع التقديري"),
        help_text=_("يُستخدم كوزن في توزيع تكلفة الأب على الأبناء."),
    )
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default=STATUS_IN_STOCK,
        db_index=True, verbose_name=_("الحالة"),
    )

    # 🔗 تكامل اختياري مع الـ ERP — لو الابن بقى قطعة قابلة للبيع في الكتالوج
    product = models.ForeignKey(
        Product, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='disassembly_items', verbose_name=_("قطعة الكتالوج المرتبطة"),
    )
    branch = models.ForeignKey(
        Branch, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='disassembly_items', verbose_name=_("الفرع"),
    )

    notes = models.TextField(blank=True, verbose_name=_("ملاحظات"))
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("عنصر مخزون (قابل للفك)")
        verbose_name_plural = _("🔩 عناصر الفك التدريجي")
        ordering = ('-created_at',)

    def __str__(self):
        return f"{self.name} [{self.sku}] ({self.get_status_display()})"

    # ----- علاقات الشجرة (recursion) -----
    @property
    def source_result(self):
        """سطر النتيجة اللي وَلَّد العنصر ده (لو هو ابن). None لو جذر."""
        return self.produced_as.first()

    @property
    def parent_item(self):
        """العنصر الأب المباشر (لو موجود)."""
        res = self.source_result
        return res.event.parent_item if res else None

    @property
    def depth(self):
        """عمق العنصر في شجرة الفك (0 = جذر / نص كت)."""
        depth, node, guard = 0, self, 0
        while node.parent_item is not None and guard < 50:
            depth += 1
            node = node.parent_item
            guard += 1
        return depth

    @property
    def can_disassemble(self):
        return self.status == self.STATUS_IN_STOCK


class DisassemblyEvent(models.Model):
    """حدث فك: بيسجّل تفكيك عنصر أب لعناصر أبناء + إيراد الخردة."""

    parent_item = models.ForeignKey(
        InventoryItem, on_delete=models.PROTECT, related_name='disassembly_events',
        verbose_name=_("العنصر الأب"),
    )
    date = models.DateTimeField(default=timezone.now, verbose_name=_("تاريخ الفك"))
    total_scrap_revenue = models.DecimalField(
        **MONEY, default=ZERO, validators=[MinValueValidator(ZERO)],
        verbose_name=_("إجمالي إيراد الخردة"),
        help_text=_("إيراد بيع البقايا/الحديد الخردة — يُخصم من تكلفة الأب قبل التوزيع."),
    )

    # قيم مجمّدة وقت التنفيذ (للتدقيق المالي)
    is_executed = models.BooleanField(default=False, verbose_name=_("تم التنفيذ؟"))
    executed_at = models.DateTimeField(null=True, blank=True, verbose_name=_("وقت التنفيذ"))
    parent_cost_snapshot = models.DecimalField(
        **MONEY, null=True, blank=True, editable=False,
        verbose_name=_("تكلفة الأب وقت التنفيذ"),
    )
    adjusted_parent_cost = models.DecimalField(
        **MONEY, null=True, blank=True, editable=False,
        verbose_name=_("تكلفة الأب المعدّلة (بعد الخردة)"),
    )

    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        verbose_name=_("نفّذها"),
    )
    notes = models.TextField(blank=True, verbose_name=_("ملاحظات"))
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("حدث فك")
        verbose_name_plural = _("🔧 أحداث الفك التدريجي")
        ordering = ('-date',)

    def __str__(self):
        state = _("منفّذ") if self.is_executed else _("مسودة")
        return f"فك #{self.pk} — {self.parent_item.name} ({state})"

    @property
    def total_estimated_sales(self):
        return self.results.aggregate(
            s=models.Sum('estimated_sales_price'))['s'] or ZERO

    @property
    def total_allocated_cost(self):
        return self.results.aggregate(
            s=models.Sum('allocated_cost'))['s'] or ZERO


class DisassemblyResult(models.Model):
    """العناصر الأبناء الناتجة من حدث فك معيّن + تكلفتها المخصّصة."""

    event = models.ForeignKey(
        DisassemblyEvent, on_delete=models.CASCADE, related_name='results',
        verbose_name=_("حدث الفك"),
    )
    child_item = models.ForeignKey(
        InventoryItem, on_delete=models.PROTECT, related_name='produced_as',
        verbose_name=_("العنصر الابن"),
    )
    estimated_sales_price = models.DecimalField(
        **MONEY, default=ZERO, validators=[MinValueValidator(ZERO)],
        verbose_name=_("سعر البيع التقديري"),
    )
    allocated_cost = models.DecimalField(
        **MONEY, default=ZERO, editable=False,
        verbose_name=_("التكلفة المخصّصة"),
    )

    class Meta:
        verbose_name = _("ناتج فك")
        verbose_name_plural = _("نواتج الفك")
        constraints = [
            models.UniqueConstraint(
                fields=['event', 'child_item'], name='uniq_event_child'),
        ]

    def __str__(self):
        return f"{self.child_item.name} ← فك #{self.event_id}"


# =============================================================================
# 📋 قوالب الفك (Reverse BOM) — عشان مانكتبش الأبناء يدوي كل مرة
# =============================================================================
# المحرك الشائع (زي N20) بيتفكّك دايماً لنفس القطع تقريباً. القالب بيخزّن
# القائمة القياسية مرة واحدة، والمستخدم يحمّلها في مسودة حدث فك ويعدّل
# (يشيل قطعة تالفة أو يظبط سعر) قبل ما يعتمد.
class DisassemblyTemplate(models.Model):
    """قالب فك قياسي قابل لإعادة الاستخدام (Reverse BOM)."""

    name = models.CharField(
        max_length=200, unique=True,
        verbose_name=_("اسم القالب"),
        help_text=_("مثال: تفكيك محرك N20 القياسي"),
    )
    engine_code = models.CharField(
        max_length=100, blank=True, db_index=True,
        verbose_name=_("كود المحرك / الموديل"),
        help_text=_("اختياري — لتصفية القوالب حسب المحرك (N20, B48...)."),
    )
    default_scrap_revenue = models.DecimalField(
        **MONEY, default=ZERO, validators=[MinValueValidator(ZERO)],
        verbose_name=_("إيراد الخردة الافتراضي"),
    )
    is_active = models.BooleanField(default=True, verbose_name=_("نشط"))
    notes = models.TextField(blank=True, verbose_name=_("ملاحظات"))
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("قالب فك")
        verbose_name_plural = _("📋 قوالب الفك (Reverse BOM)")
        ordering = ('name',)

    def __str__(self):
        code = f" [{self.engine_code}]" if self.engine_code else ""
        return f"{self.name}{code}"

    @property
    def total_default_weight(self):
        """مجموع الأوزان النسبية (لو القالب معرّف بالنِّسَب)."""
        return self.items.aggregate(s=models.Sum('weight_percentage'))['s'] or ZERO


class TemplateItem(models.Model):
    """سطر قياسي داخل قالب فك: اسم القطعة + وزنها (سعر تقديري أو نسبة %)."""

    template = models.ForeignKey(
        DisassemblyTemplate, on_delete=models.CASCADE, related_name='items',
        verbose_name=_("القالب"),
    )
    part_name = models.CharField(max_length=200, verbose_name=_("اسم القطعة"))
    default_estimated_sales_price = models.DecimalField(
        **MONEY, default=ZERO, validators=[MinValueValidator(ZERO)],
        verbose_name=_("سعر البيع التقديري الافتراضي"),
    )
    # بديل للسعر التقديري: نسبة وزن مئوية من قيمة الأب (0–100)
    weight_percentage = models.DecimalField(
        max_digits=6, decimal_places=3, default=ZERO,
        validators=[MinValueValidator(ZERO)],
        verbose_name=_("نسبة الوزن %"),
        help_text=_("بديل للسعر — لو > 0 تُستخدم لتقدير السعر من تكلفة الأب."),
    )
    # ربط اختياري بقطعة كتالوج جاهزة (لتوليد SKU/الاسم تلقائياً)
    product = models.ForeignKey(
        Product, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='template_items', verbose_name=_("قطعة الكتالوج"),
    )
    sku_prefix = models.CharField(
        max_length=60, blank=True,
        verbose_name=_("بادئة SKU للأبناء"),
        help_text=_("اختياري — يُدمج مع مرجع الأب لتوليد SKU فريد للابن."),
    )
    sort_order = models.PositiveIntegerField(default=0, verbose_name=_("الترتيب"))

    class Meta:
        verbose_name = _("بند قالب")
        verbose_name_plural = _("بنود القوالب")
        ordering = ('sort_order', 'id')

    def __str__(self):
        return f"{self.part_name} ({self.template.name})"
