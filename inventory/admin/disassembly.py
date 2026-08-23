# 🔩 لوحة تحكم الفك التدريجي المتعدد المستويات
from django.contrib import admin, messages
from django.core.exceptions import ValidationError
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _

from ..models import (DisassemblyEvent, DisassemblyResult, DisassemblyTemplate,
                      InventoryItem, TemplateItem)
from ..services import DisassemblyService


@admin.register(InventoryItem)
class InventoryItemAdmin(admin.ModelAdmin):
    list_display = ('sku', 'name', 'status', 'cost', 'estimated_sales_price',
                    'depth_col', 'parent_col', 'created_at')
    list_filter = ('status', 'branch', 'created_at')
    search_fields = ('sku', 'name')
    raw_id_fields = ('product', 'branch')
    readonly_fields = ('created_at', 'updated_at')

    def depth_col(self, obj):
        return f"L{obj.depth}"
    depth_col.short_description = _("المستوى")

    def parent_col(self, obj):
        p = obj.parent_item
        return p.name if p else '—'
    parent_col.short_description = _("الأب المباشر")


class DisassemblyResultInline(admin.TabularInline):
    model = DisassemblyResult
    extra = 0
    raw_id_fields = ('child_item',)
    fields = ('child_item', 'estimated_sales_price', 'allocated_cost')
    readonly_fields = ('allocated_cost',)


@admin.register(DisassemblyEvent)
class DisassemblyEventAdmin(admin.ModelAdmin):
    list_display = ('id', 'parent_item', 'date', 'total_scrap_revenue',
                    'executed_badge', 'total_allocated_cost')
    list_filter = ('is_executed', 'date')
    search_fields = ('parent_item__sku', 'parent_item__name')
    raw_id_fields = ('parent_item', 'created_by')
    readonly_fields = ('is_executed', 'executed_at', 'parent_cost_snapshot',
                       'adjusted_parent_cost', 'created_at')
    inlines = [DisassemblyResultInline]
    actions = ['run_disassembly']

    def executed_badge(self, obj):
        if obj.is_executed:
            return format_html('<b style="color:#0a0">✅ منفّذ</b>')
        return format_html('<span style="color:#c80">📝 مسودة</span>')
    executed_badge.short_description = _("الحالة")

    @admin.action(description=_("🔩 نفّذ توزيع التكلفة (الفك)"))
    def run_disassembly(self, request, queryset):
        ok = 0
        for event in queryset:
            try:
                DisassemblyService.execute_disassembly(event)
                ok += 1
            except ValidationError as exc:
                self.message_user(
                    request, f"فك #{event.pk}: {getattr(exc, 'message', exc)}",
                    level=messages.ERROR)
        if ok:
            self.message_user(request, f"تم تنفيذ {ok} حدث فك بنجاح ✅",
                              level=messages.SUCCESS)


class TemplateItemInline(admin.TabularInline):
    model = TemplateItem
    extra = 1
    raw_id_fields = ('product',)
    fields = ('sort_order', 'part_name', 'default_estimated_sales_price',
              'weight_percentage', 'product', 'sku_prefix')


@admin.register(DisassemblyTemplate)
class DisassemblyTemplateAdmin(admin.ModelAdmin):
    list_display = ('name', 'engine_code', 'items_count', 'default_scrap_revenue',
                    'is_active', 'updated_at')
    list_filter = ('is_active', 'engine_code')
    search_fields = ('name', 'engine_code')
    inlines = [TemplateItemInline]

    def items_count(self, obj):
        return obj.items.count()
    items_count.short_description = _("عدد البنود")
