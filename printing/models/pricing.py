"""💲 المطابع — قايمة الأسعار وحاسبة التسعير (PrintPriceRule, PrintPriceTier).

🆕 الأسعار كانت بتتكتب بالإيد في كل فاتورة/عرض سعر — كل موظف بسعر، والعميل بياخد سعرين لنفس الشغل.
زي الأنظمة العالمية (Printavo/Ordant/PrintSmith): كل منتج ليه «مصفوفة أسعار» بشرايح كمية
(كل ما الكمية تزيد سعر الوحدة يقل) + رسوم تجهيز ثابتة (زنكات/قص/إعداد ماكينة) + حد أدنى للطلب.
الفاتورة السريعة وعرض السعر والمتجر الأونلاين بيحسبوا من نفس القاعدة (printing.pricing).
"""
from decimal import ROUND_HALF_UP, Decimal

from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.utils.translation import gettext_lazy as _

from .production import ProductType

CENT = Decimal('0.01')


class PrintPriceRule(models.Model):
    name = models.CharField(max_length=150, unique=True, verbose_name=_("المنتج"),
                            help_text=_("زي ما بيتكتب في الفاتورة — مثال: كارت بزنس 350 جم سوفت تاتش"))
    product_type = models.ForeignKey(ProductType, on_delete=models.SET_NULL, null=True, blank=True,
                                     verbose_name=_("نوع البند"))
    unit_label = models.CharField(max_length=30, default='قطعة', verbose_name=_("الوحدة"),
                                  help_text=_("قطعة / كارت / متر / نسخة"))
    setup_fee = models.DecimalField(max_digits=10, decimal_places=2, default=0,
                                    validators=[MinValueValidator(Decimal('0'))],
                                    verbose_name=_("رسوم التجهيز"),
                                    help_text=_("مبلغ ثابت على الطلب كله (زنكات، إعداد ماكينة، قص) — بيتوزّع على الوحدات."))
    min_total = models.DecimalField(max_digits=10, decimal_places=2, default=0,
                                    validators=[MinValueValidator(Decimal('0'))],
                                    verbose_name=_("أقل سعر للطلب"),
                                    help_text=_("لو الحساب طلع أقل من كده بيتحسب الحد الأدنى. صفر = مفيش."))
    min_quantity = models.PositiveIntegerField(default=1, verbose_name=_("أقل كمية"))
    description = models.TextField(blank=True, verbose_name=_("وصف للعميل"),
                                   help_text=_("بيظهر في المتجر الأونلاين (الخامة، المقاس، مدة التنفيذ)."))
    is_public = models.BooleanField(default=False, verbose_name=_("يظهر في المتجر الأونلاين"))
    is_active = models.BooleanField(default=True, verbose_name=_("شغّال"))
    sort_order = models.PositiveSmallIntegerField(default=0, verbose_name=_("الترتيب"))
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("سعر منتج")
        verbose_name_plural = _("💲 قايمة الأسعار (حاسبة التسعير)")
        ordering = ['sort_order', 'name']

    def __str__(self):
        return self.name

    def unit_price_for(self, qty):
        """سعر الوحدة من الشريحة: أعلى «من كمية» أقل من أو يساوي الكمية."""
        tier = None
        for t in self.tiers.all():          # مترتبة بـ min_qty
            if t.min_qty <= qty:
                tier = t
        if tier is None:
            raise ValidationError(_("مفيش سعر لكمية %(q)s من «%(n)s».") % {'q': qty, 'n': self.name})
        return tier.unit_price

    def quote(self, qty):
        """(سعر الوحدة الفعلي، الإجمالي، التفاصيل) للكمية دي."""
        qty = int(qty)
        if qty < max(1, self.min_quantity):
            raise ValidationError(_("أقل كمية من «%(n)s» هي %(m)s.") % {'n': self.name, 'm': max(1, self.min_quantity)})
        base_unit = self.unit_price_for(qty)
        total = base_unit * qty + self.setup_fee
        floor_applied = self.min_total and total < self.min_total
        if floor_applied:
            total = self.min_total
        total = total.quantize(CENT, rounding=ROUND_HALF_UP)
        # سعر الوحدة في الفاتورة = الإجمالي ÷ الكمية (التجهيز والحد الأدنى متوزّعين عليه)
        unit = (total / qty).quantize(CENT, rounding=ROUND_HALF_UP)
        return {'unit_price': unit, 'total': (unit * qty).quantize(CENT), 'base_unit': base_unit,
                'setup_fee': self.setup_fee, 'min_applied': bool(floor_applied)}

    def as_dict(self):
        return {'id': self.pk, 'name': self.name, 'unit': self.unit_label, 'setup': str(self.setup_fee),
                'min_total': str(self.min_total), 'min_qty': max(1, self.min_quantity),
                'tiers': [[t.min_qty, str(t.unit_price)] for t in self.tiers.all()]}


class PrintPriceTier(models.Model):
    rule = models.ForeignKey(PrintPriceRule, on_delete=models.CASCADE, related_name='tiers')
    min_qty = models.PositiveIntegerField(verbose_name=_("من كمية"))
    unit_price = models.DecimalField(max_digits=10, decimal_places=3,
                                     validators=[MinValueValidator(Decimal('0'))],
                                     verbose_name=_("سعر الوحدة"))

    class Meta:
        verbose_name = _("شريحة سعر")
        verbose_name_plural = _("شرايح الكمية (كل ما الكمية تزيد السعر يقل)")
        ordering = ['min_qty']
        constraints = [models.UniqueConstraint(fields=['rule', 'min_qty'], name='print_price_tier_unique_qty')]

    def __str__(self):
        return f"{self.min_qty}+ × {self.unit_price}"
