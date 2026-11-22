"""💲 لوحة المطابع — قايمة الأسعار بشرايح الكمية (حاسبة التسعير)."""
from django.contrib import admin, messages
from django.urls import reverse
from django.utils.html import format_html, format_html_join
from django.utils.translation import gettext, gettext_lazy as _

from ..models import PrintPriceRule, PrintPriceTier
from ._common import PrintSecureAdmin


class PrintPriceTierInline(admin.TabularInline):
    model = PrintPriceTier
    extra = 3
    fields = ('min_qty', 'unit_price')


@admin.register(PrintPriceRule)
class PrintPriceRuleAdmin(PrintSecureAdmin):
    list_display = ('name', 'unit_label', 'tiers_display', 'setup_fee', 'min_total', 'is_public', 'is_active')
    list_editable = ('is_public', 'is_active')
    list_filter = ('is_active', 'is_public', 'product_type')
    search_fields = ('name',)
    autocomplete_fields = ('product_type',)
    inlines = [PrintPriceTierInline]
    readonly_fields = ('examples',)
    fieldsets = (
        (None, {'fields': ('name', 'product_type', 'unit_label', 'min_quantity', 'setup_fee', 'min_total')}),
        (_("المتجر الأونلاين"), {'fields': ('is_public', 'description', 'sort_order', 'is_active')}),
        (_("أمثلة من الحاسبة"), {'fields': ('examples',)}),
    )

    def changelist_view(self, request, extra_context=None):
        if request.method == 'GET' and self.model.objects.filter(is_public=True, is_active=True).exists():
            url = reverse('printing:print_shop')
            messages.info(request, format_html('{}: <a href="{}" target="_blank"><b dir="ltr">{}</b></a>',
                                               gettext('🛒 متجر المطبعة الأونلاين'), url, request.build_absolute_uri(url)))
        return super().changelist_view(request, extra_context)

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related('tiers')

    def tiers_display(self, obj):
        tiers = list(obj.tiers.all())
        if not tiers:
            return format_html('<b style="color:#dc2626;">{}</b>', gettext('مفيش شرايح'))
        return format_html_join(' · ', '<span dir="ltr">{}+ = {}</span>',
                                ((t.min_qty, f"{t.unit_price:g}") for t in tiers))
    tiers_display.short_description = _("الشرايح")

    def examples(self, obj):
        if not obj or not obj.pk or not obj.tiers.exists():
            return gettext("احفظ المنتج بشرايحه الأول.")
        rows = []
        for t in obj.tiers.all():
            for qty in (t.min_qty, t.min_qty * 2):
                try:
                    q = obj.quote(qty)
                except Exception:
                    continue
                rows.append((qty, f"{q['unit_price']:,.2f}", f"{q['total']:,.2f}"))
        return format_html('<table class="table table-sm" style="max-width:420px;"><tr><th>{}</th><th>{}</th><th>{}</th></tr>{}</table>',
                           gettext('الكمية'), gettext('سعر الوحدة'), gettext('الإجمالي'),
                           format_html_join('', '<tr><td dir="ltr">{}</td><td dir="ltr">{}</td><td dir="ltr"><b>{}</b></td></tr>', rows))
    examples.short_description = _("أمثلة")
