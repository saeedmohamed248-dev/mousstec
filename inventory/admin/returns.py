# 🛡️ لوحة تحكم حارس المرتجعات
from django.contrib import admin
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from django.utils.translation import gettext_lazy as _

from ..models import PartReturnGuard, PartReturnPhoto


class PartReturnPhotoInline(admin.TabularInline):
    model = PartReturnPhoto
    extra = 0
    fields = ('stage', 'thumb', 'source', 'sha256', 'uploaded_at')
    readonly_fields = ('thumb', 'sha256', 'uploaded_at')

    def thumb(self, obj):
        url = ''
        if obj.image:
            try:
                url = obj.image.url
            except Exception:
                url = ''
        url = url or obj.image_url_external
        if url:
            return format_html('<img src="{}" style="max-height:90px;border-radius:6px" />', url)
        return '—'
    thumb.short_description = _("معاينة")


@admin.register(PartReturnGuard)
class PartReturnGuardAdmin(admin.ModelAdmin):
    list_display = ('id', 'part_number', 'status', 'verdict_badge', 'match_score_col',
                    'source', 'external_ref', 'customer', 'created_at')
    list_filter = ('status', 'source', 'created_at')
    search_fields = ('part_number', 'external_ref', 'customer__phone', 'customer__name',
                     'public_token')
    date_hierarchy = 'created_at'
    raw_id_fields = ('product', 'invoice_item', 'original_invoice', 'customer')
    readonly_fields = ('public_token', 'dispatch_analyzed_at', 'verdict_at',
                       'created_at', 'updated_at', 'verdict_pretty')
    inlines = [PartReturnPhotoInline]
    fieldsets = (
        (_("الأساسيات"), {'fields': (
            'product', 'part_number', 'source', 'external_ref', 'public_token',
            'invoice_item', 'original_invoice', 'customer', 'status')}),
        (_("بصمة الصرف"), {'fields': ('dispatch_fingerprint', 'dispatch_analyzed_at')}),
        (_("حكم المرتجع"), {'fields': ('verdict_pretty', 'verdict', 'return_fingerprint',
                                       'verdict_at')}),
        (_("أخرى"), {'fields': ('notes', 'created_at', 'updated_at')}),
    )

    def verdict_badge(self, obj):
        r = obj.is_returnable
        if r is True:
            return format_html('<b style="color:#0a0">✅ تنفع ترجع</b>')
        if r is False:
            return format_html('<b style="color:#c00">❌ مش هترجع</b>')
        return format_html('<span style="color:#c80">⏳ مراجعة</span>')
    verdict_badge.short_description = _("الحكم")

    def match_score_col(self, obj):
        s = obj.match_score
        return f"{s}%" if s is not None else '—'
    match_score_col.short_description = _("التطابق")

    def verdict_pretty(self, obj):
        reasons = obj.rejection_reasons
        if not reasons:
            return '—'
        items = ''.join(f'<li>{r}</li>' for r in reasons)
        return mark_safe(f'<ul style="margin:0;padding-inline-start:18px">{items}</ul>')
    verdict_pretty.short_description = _("الأسباب")
