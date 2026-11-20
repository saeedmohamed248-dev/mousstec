"""📦 المطابع — الخامات واستهلاكها وتسويات المخزون."""
from decimal import Decimal
from django.db import models
from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.core.validators import MinValueValidator
from django.utils.translation import gettext as _t
from .customers import PrintBranch
from .production import PrintJob


# =====================================================================
# 📦 5. المخزون (خامات الطباعة)
# =====================================================================

class PrintMaterial(models.Model):
    """خامات الطباعة: ورق، حبر، فينيل، بنر، إلخ"""
    CATEGORY_CHOICES = (
        ('paper', _('ورق')),
        ('ink', _('حبر')),
        ('vinyl', _('فينيل')),
        ('banner', _('بنر / فليكس')),
        ('lamination', _('تغليف / لامينيشن')),
        ('packaging', _('تغليف وتعبئة')),
        ('other', _('خامات أخرى')),
    )

    name = models.CharField(max_length=200, verbose_name=_("اسم الخامة"))
    category = models.CharField(max_length=15, choices=CATEGORY_CHOICES, default='paper', verbose_name=_("التصنيف"))
    sku = models.CharField(max_length=50, blank=True, verbose_name=_("كود الخامة"))
    unit = models.CharField(max_length=30, default='قطعة', verbose_name=_("وحدة القياس"), help_text=_("مثال: رول، ورقة، لتر، مل"))
    branch = models.ForeignKey(PrintBranch, on_delete=models.SET_NULL, null=True, blank=True, verbose_name=_("الفرع"))

    quantity = models.DecimalField(max_digits=12, decimal_places=2, default=0, validators=[MinValueValidator(Decimal('0.00'))], verbose_name=_("الكمية الحالية"))
    min_stock = models.DecimalField(max_digits=12, decimal_places=2, default=5, validators=[MinValueValidator(Decimal('0.00'))], verbose_name=_("الحد الأدنى"))
    # 4 خانات عشرية: متوسط تكلفة الفرخ/المتر بيطلع كسور (1.234) — خانتين بيضيّعوا قروش في كل شراء.
    cost_per_unit = models.DecimalField(max_digits=14, decimal_places=4, default=0, validators=[MinValueValidator(Decimal('0.00'))], verbose_name=_("تكلفة الوحدة (ج.م)"))

    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_("تاريخ الإضافة"))

    class Meta:
        verbose_name = _("خامة طباعة")
        verbose_name_plural = _("خامات الطباعة")
        ordering = ['category', 'name']

    def __str__(self):
        return f"{self.name} ({self.get_category_display()})"

    @property
    def is_low_stock(self):
        return self.quantity <= self.min_stock

    @property
    def stock_value(self):
        return self.quantity * self.cost_per_unit


class PrintJobMaterial(models.Model):
    """
    🧾 خامة اتصرفت على مهمة طباعة — بتخصم من المخزون وبتدخل في تكلفة المهمة.

    🐛 [FIX]: المخزون (ورق/فينيل/بنر) ماكانش بيتخصم من أي شغل، وتكلفته
    ماكانتش داخلة في تكلفة المهمة — فربح كل طلب كان متضخّم وعدّ المخزون
    مابيتحركش غير بالتعديل اليدوي.
    """
    job = models.ForeignKey(PrintJob, on_delete=models.CASCADE, related_name='material_usages',
                            verbose_name=_("المهمة"))
    material = models.ForeignKey(PrintMaterial, on_delete=models.PROTECT, related_name='usages',
                                 verbose_name=_("الخامة"))
    quantity = models.DecimalField(max_digits=12, decimal_places=2,
                                   validators=[MinValueValidator(Decimal('0.01'))],
                                   verbose_name=_("الكمية المصروفة"))
    unit_cost = models.DecimalField(max_digits=14, decimal_places=4, default=0, editable=False,
                                    verbose_name=_("تكلفة الوحدة وقت الصرف"))
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_("تاريخ الإضافة"))

    class Meta:
        verbose_name = _("خامة مصروفة")
        verbose_name_plural = _("الخامات المصروفة على المهام")

    def __str__(self):
        return f"{self.material} × {self.quantity} → {self.job_id}"

    @property
    def total_cost(self):
        return ((self.quantity or Decimal('0')) * (self.unit_cost or Decimal('0'))).quantize(Decimal('0.01'))

    def save(self, *args, **kwargs):
        """Deduct (or adjust) stock atomically; refuse to go below zero."""
        from django.core.exceptions import ValidationError
        from django.db import transaction as _txn
        with _txn.atomic():
            old = None
            if self.pk:
                old = PrintJobMaterial.objects.select_for_update().filter(pk=self.pk).first()
            if old is not None:
                # Give back what the previous version of this line took.
                PrintMaterial.objects.filter(pk=old.material_id).update(
                    quantity=models.F('quantity') + old.quantity)
            mat = PrintMaterial.objects.select_for_update().get(pk=self.material_id)
            if mat.quantity < self.quantity:
                raise ValidationError(
                    _t("المخزون من «%(name)s» مش كفاية: المتاح %(have)s %(unit)s، المطلوب %(need)s.") % {
                        'name': mat.name, 'have': mat.quantity, 'unit': mat.unit, 'need': self.quantity}
                )
            if old is None or old.material_id != self.material_id or not self.unit_cost:
                self.unit_cost = mat.cost_per_unit
            PrintMaterial.objects.filter(pk=mat.pk).update(quantity=models.F('quantity') - self.quantity)
            super().save(*args, **kwargs)
            # A job already completed has a frozen cost snapshot — keep it true.
            delta = self.total_cost - (old.total_cost if old is not None else Decimal('0'))
            _shift_job_snapshot(self.job_id, delta)


def _shift_job_snapshot(job_id, cost_delta):
    """Add ``cost_delta`` to a completed job's frozen cost (and take it off profit)."""
    if not cost_delta:
        return
    PrintJob.objects.filter(pk=job_id, is_complete=True, completed_at__isnull=False).update(
        actual_cost=models.F('actual_cost') + cost_delta,
        actual_profit=models.F('actual_profit') - cost_delta,
    )


# =====================================================================
# 📦 10. تسويات المخزون (جرد / تالف) — الطريق الوحيد لتعديل الكمية بالإيد
# =====================================================================

class PrintStockAdjustment(models.Model):
    """
    تسوية كمية خامة (عجز/زيادة جرد، تالف…) — بتتقيّد فروقات جرد بتكلفة الخامة.

    🐛 [FIX]: الكمية كانت بتتكتب بالإيد في صفحة الخامة، فالمخزون بيتغيّر من غير
    أثر ولا قيمة ولا سبب. دلوقتي الكمية بتتحرك بس من: الشراء، الصرف على الشغل،
    أو تسوية مسجّلة هنا.
    """
    REASON_CHOICES = (
        ('count', _('جرد — فرق عدّ')),
        ('damage', _('تالف / هالك')),
        ('other', _('أخرى')),
    )
    material = models.ForeignKey(PrintMaterial, on_delete=models.PROTECT, related_name='adjustments',
                                 verbose_name=_("الخامة"))
    quantity_change = models.DecimalField(max_digits=12, decimal_places=2, verbose_name=_("التغيير في الكمية"),
                                          help_text=_("موجب = زيادة، سالب = عجز/تالف."))
    unit_cost = models.DecimalField(max_digits=14, decimal_places=4, default=0, editable=False,
                                    verbose_name=_("تكلفة الوحدة وقت التسوية"))
    reason = models.CharField(max_length=10, choices=REASON_CHOICES, default='count', verbose_name=_("السبب"))
    notes = models.CharField(max_length=300, blank=True, verbose_name=_("ملاحظات"))
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='+', verbose_name=_("بواسطة"))
    created_at = models.DateTimeField(default=timezone.now, verbose_name=_("التاريخ"))

    class Meta:
        verbose_name = _("تسوية مخزن")
        verbose_name_plural = _("تسويات المخزن (جرد / تالف)")
        ordering = ['-created_at']

    def __str__(self):
        sign = '+' if self.quantity_change > 0 else ''
        return f"{self.material} {sign}{self.quantity_change}"

    @property
    def value(self):
        """القيمة بإشارتها: موجب = زيادة في المخزون، سالب = عجز."""
        return ((self.quantity_change or Decimal('0')) * (self.unit_cost or Decimal('0'))).quantize(Decimal('0.01'))

    def save(self, *args, **kwargs):
        from django.db import transaction
        if self.pk:
            raise ValidationError(_("التسوية مابتتعدّلش — امسحها واعمل واحدة جديدة."))
        if not self.quantity_change:
            raise ValidationError(_("التغيير في الكمية مينفعش يبقى صفر."))
        with transaction.atomic():
            mat = PrintMaterial.objects.select_for_update().get(pk=self.material_id)
            if mat.quantity + self.quantity_change < 0:
                raise ValidationError(
                    _("المخزون من «%(m)s» مش كفاية: المتاح %(q)s %(u)s.")
                    % {'m': mat.name, 'q': mat.quantity, 'u': mat.unit})
            self.unit_cost = mat.cost_per_unit
            PrintMaterial.objects.filter(pk=mat.pk).update(quantity=models.F('quantity') + self.quantity_change)
            super().save(*args, **kwargs)
