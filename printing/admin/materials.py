"""📦 لوحة المطابع — الخامات وتسويات المخزن."""

from django.contrib import admin, messages
from django.utils.html import format_html
from django.utils.translation import gettext, gettext_lazy as _
from erp_core.localization import current_tenant_symbol as _cur_sym
from django import forms
from django.core.exceptions import ValidationError

from ..models import (
    PrintMaterial, PrintStockAdjustment,
)
from ._common import PrintSecureAdmin, _money


# =====================================================================
# 📦 خامات الطباعة
# =====================================================================

@admin.register(PrintMaterial)
class PrintMaterialAdmin(PrintSecureAdmin):
    list_display = ('name', 'category', 'quantity_display', 'cost_display', 'stock_value_display', 'stock_alert',
                    'card_link')
    list_filter = ('category', 'branch')

    def card_link(self, obj):
        return format_html('<a href="/printing/material/{}/card/">📇 {}</a>', obj.pk, gettext('كارت الصنف'))
    card_link.short_description = _("كارت الصنف")
    search_fields = ('name', 'sku')
    list_select_related = ('branch',)

    def get_readonly_fields(self, request, obj=None):
        # 🐛 [FIX]: الكمية والتكلفة كانوا بيتكتبوا بالإيد فالمخزون بيتغيّر من غير
        #    مستند ولا قيد. بعد الإضافة (= المخزون الافتتاحي) بيتحركوا من فواتير
        #    الشراء والصرف على الشغل وتسويات المخزن بس.
        ro = list(super().get_readonly_fields(request, obj))
        if obj and obj.pk:
            ro += ['quantity', 'cost_per_unit', 'stock_moves']
        return ro

    def get_fields(self, request, obj=None):
        fields = list(super().get_fields(request, obj))
        if obj and obj.pk:
            fields.append('stock_moves')
        return fields

    def stock_moves(self, obj):
        return format_html(
            '<a href="/secure-portal/printing/materialpurchase/add/">📥 {}</a> · '
            '<a href="/secure-portal/printing/printstockadjustment/add/?material={}">⚖️ {}</a> · '
            '<a href="/printing/material/{}/card/">📇 {}</a>',
            gettext('فاتورة شراء'), obj.pk, gettext('تسوية جرد / تالف'), obj.pk, gettext('كارت الصنف'))
    stock_moves.short_description = _("تحريك المخزون")

    def cost_display(self, obj):
        return f"{float(obj.cost_per_unit):,.4f}".rstrip('0').rstrip('.')
    cost_display.short_description = _("تكلفة الوحدة")

    def quantity_display(self, obj):
        return format_html('<b>{}</b> {}', f"{float(obj.quantity):,.1f}", obj.unit)
    quantity_display.short_description = _("الكمية")

    def stock_value_display(self, obj):
        return format_html('<b>{}</b> {}', f"{float(obj.stock_value):,.2f}", _cur_sym())
    stock_value_display.short_description = _("القيمة")

    def stock_alert(self, obj):
        if obj.is_low_stock:
            return format_html('<span style="background:#ef4444;color:white;padding:2px 8px;border-radius:8px;font-size:11px;font-weight:bold;">⚠️ {}</span>', gettext('منخفض'))
        return format_html('<span style="color:#10b981;">✅ {}</span>', gettext('متوفر'))
    stock_alert.short_description = _("المخزون")


# =====================================================================
# ⚖️ تسويات المخزن
# =====================================================================

class PrintStockAdjustmentForm(forms.ModelForm):
    counted_quantity = forms.DecimalField(
        required=False, max_digits=12, decimal_places=2, label=_("الكمية الفعلية بعد الجرد"),
        help_text=_("لو عملت جرد اكتب الموجود فعلاً — السيستم يحسب الفرق لوحده."))

    class Meta:
        model = PrintStockAdjustment
        fields = ('material', 'counted_quantity', 'quantity_change', 'reason', 'notes')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # 🐛 [FIX]: التسوية المحفوظة بتتفتح «عرض بس» (مابتتعدّلش) — الخانة مش في الفورم
        #    ساعتها، فـ self.fields['quantity_change'] كان بيقع KeyError ⇒ 500 من «كارت الصنف».
        if 'quantity_change' in self.fields:
            self.fields['quantity_change'].required = False

    def clean(self):
        data = super().clean()
        material, counted = data.get('material'), data.get('counted_quantity')
        if material is not None and counted is not None:
            data['quantity_change'] = counted - material.quantity
            self.instance.quantity_change = data['quantity_change']
        change = data.get('quantity_change')
        if not change:
            raise ValidationError(_("اكتب الكمية الفعلية بعد الجرد أو التغيير في الكمية (ومينفعش يبقى صفر)."))
        if material is not None and material.quantity + change < 0:
            raise ValidationError(_("المتاح من «%(m)s» %(q)s %(u)s بس.")
                                  % {'m': material.name, 'q': material.quantity, 'u': material.unit})
        return data


@admin.register(PrintStockAdjustment)
class PrintStockAdjustmentAdmin(PrintSecureAdmin):
    form = PrintStockAdjustmentForm
    list_display = ('material', 'change_display', 'value_display', 'reason', 'notes', 'created_by', 'created_at')
    list_filter = ('reason', 'material', 'created_at')
    search_fields = ('material__name', 'notes')
    list_select_related = ('material', 'created_by')

    def has_change_permission(self, request, obj=None):
        # التسوية مابتتعدّلش — تتمسح (الكمية ترجع) وتتعمل من جديد.
        return obj is None and super().has_change_permission(request, obj)

    def save_model(self, request, obj, form, change):
        if not obj.created_by_id:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)

    def delete_model(self, request, obj):
        try:
            super().delete_model(request, obj)
        except ValidationError as exc:
            messages.error(request, '؛ '.join(exc.messages))

    def delete_queryset(self, request, queryset):
        for obj in queryset:
            self.delete_model(request, obj)

    def change_display(self, obj):
        color = '#10b981' if obj.quantity_change > 0 else '#ef4444'
        sign = '+' if obj.quantity_change > 0 else ''
        return format_html('<b style="color:{};">{}{}</b> {}', color, sign, obj.quantity_change, obj.material.unit)
    change_display.short_description = _("التغيير")

    def value_display(self, obj):
        return _money(obj.value)
    value_display.short_description = _("القيمة")
