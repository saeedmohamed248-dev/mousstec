"""🧾 لوحة المطابع — عروض الأسعار، البروفات، الإشعارات الدائنة."""
from decimal import Decimal

from django.contrib import admin, messages
from django.urls import reverse
from django.utils.html import format_html
from django.utils.translation import gettext, gettext_lazy as _
from erp_core.localization import current_tenant_symbol as _cur_sym
from django.db import connection
from django import forms
from django.core.exceptions import ValidationError

from ..models import (
    PrintOrder, PrintTreasury, PriceQuotation, QuotationLine,
    PrintProof, PrintCreditNote,
)
from ._common import PrintSecureAdmin, _SafeSaveDeleteMixin, _amount_html
from .production import _proof_links, _wa_number


# =====================================================================
# 💰 عروض الأسعار (Quotations) Admin
# =====================================================================

class QuotationLineInline(admin.TabularInline):
    model = QuotationLine
    fields = ('description', 'quantity', 'unit_price', 'line_total', 'sort_order')
    readonly_fields = ('line_total',)
    extra = 1


@admin.register(PriceQuotation)
class PriceQuotationAdmin(PrintSecureAdmin):
    list_display = ('quote_number', 'title', 'customer_col', 'total_col', 'status_badge', 'valid_until', 'share_link', 'created_at')
    list_filter = ('status', 'created_at', 'valid_until')
    search_fields = ('quote_number', 'title', 'customer_name', 'customer__name', 'customer_phone')
    # 🐛 [FIX]: «بواسطة» كان حقل مطلوب ومستخبي في قسم مقفول — فعرض السعر الجديد
    # مابيتحفظش («هذا الحقل مطلوب») والمستخدم مش شايف هو فين. بيتسجّل لوحده.
    readonly_fields = ('quote_number', 'share_token', 'subtotal', 'total', 'sent_at', 'responded_at',
                       'converted_order', 'created_by', 'quote_actions')
    inlines = [QuotationLineInline]
    actions = ['convert_to_order']

    def get_urls(self):
        from django.urls import path
        return [path('<int:quote_id>/convert/', self.admin_site.admin_view(self.convert_view),
                     name='printing_pricequotation_convert')] + super().get_urls()

    def convert_view(self, request, quote_id):
        """POST from the quote page's «حوّل لطلب» button → the new order's page."""
        from django.shortcuts import get_object_or_404, redirect
        from printing.views.finance import convert_quotation
        quote = get_object_or_404(PriceQuotation, pk=quote_id)
        back = reverse('admin:printing_pricequotation_change', args=[quote.pk])
        if request.method != 'POST' or not self.has_change_permission(request, quote):
            return redirect(back)
        try:
            order = convert_quotation(quote, user=request.user)
        except ValidationError as exc:
            self.message_user(request, '؛ '.join(exc.messages), messages.ERROR)
            return redirect(back)
        self.message_user(request, gettext("اتعمل طلب #%(no)s من العرض — كمّل بيانات الإنتاج والدفعات.")
                          % {'no': order.order_number}, messages.SUCCESS)
        return redirect(reverse('admin:printing_printorder_change', args=[order.pk]))

    def quote_actions(self, obj):
        if not obj or not obj.pk:
            return '—'
        from urllib.parse import quote as _q
        link = self._public_url(obj)
        btn = ('display:inline-block;margin:2px 4px 2px 0;padding:6px 12px;border-radius:8px;border:0;cursor:pointer;'
               'text-decoration:none;font-weight:700;font-size:0.82rem;color:#fff;font-family:inherit;background:{}')
        parts = [format_html('<a href="{}" target="_blank" style="' + btn + '">🔗 {}</a>', link, '#8b5cf6', gettext('معاينة رابط العميل'))]
        wa = _wa_number(obj.customer_whatsapp or obj.customer_phone
                        or (obj.customer.whatsapp or obj.customer.phone if obj.customer else ''))
        if wa:
            text = f"أهلاً {obj.customer_display}، ده عرض السعر «{obj.title}» بإجمالي {obj.total:,.2f} {_cur_sym()}:\n{link}"
            parts.append(format_html('<a href="https://wa.me/{}?text={}" target="_blank" rel="noopener" style="' + btn + '">💬 {}</a>',
                                     wa, _q(text), '#25d366', gettext('ابعته للعميل واتساب')))
        if obj.status == 'accepted' and not obj.converted_order_id:
            parts.append(format_html(
                '<button type="submit" formaction="{}" formmethod="post" formnovalidate style="' + btn + '">🔁 {}</button>',
                reverse('admin:printing_pricequotation_convert', args=[obj.pk]), '#10b981', gettext('حوّل لطلب طباعة')))
        if obj.converted_order_id:
            parts.append(format_html('<a href="{}" style="' + btn + '">📋 {}</a>',
                                     reverse('admin:printing_printorder_change', args=[obj.converted_order_id]),
                                     '#0f172a', gettext('الطلب #%(no)s') % {'no': obj.converted_order.order_number}))
        return format_html(''.join(['{}'] * len(parts)), *parts)
    quote_actions.short_description = _("إجراءات")

    @staticmethod
    def _public_url(obj):
        """Absolute link for the customer (on the shop's own domain)."""
        from django.conf import settings as _s
        path = reverse('printing:quotation_public_view', args=[obj.share_token])
        try:
            domain = connection.tenant.get_primary_domain().domain
        except Exception:
            return path
        return f"{'http' if _s.DEBUG else 'https'}://{domain}{path}"

    @admin.action(description=_("🔁 تحويل العروض المقبولة لطلبات طباعة (مهمة لكل بند)"))
    def convert_to_order(self, request, queryset):
        from django.contrib import messages
        from printing.views.finance import convert_quotation
        done, failed = [], []
        for quote in queryset:
            try:
                order = convert_quotation(quote, user=request.user)
                done.append(order.order_number)
            except ValidationError as exc:
                failed.append(f"#{quote.quote_number}: {'؛ '.join(exc.messages)}")
        if done:
            messages.success(request, gettext("اتعمل %(n)s طلب: %(list)s") % {'n': len(done), 'list': ', '.join(done)})
        for f in failed:
            messages.warning(request, f)
    fieldsets = (
        (_('بيانات العميل'), {
            'fields': ('customer', 'customer_name', 'customer_phone', 'customer_whatsapp', 'quote_actions')
        }),
        (_('محتوى العرض'), {
            'fields': ('title', 'notes', 'discount', 'tax_percent')
        }),
        (_('الحالة والصلاحية'), {
            'fields': ('status', 'valid_until', 'sent_at', 'responded_at', 'converted_order')
        }),
        (_('المراجع'), {
            'classes': ('collapse',),
            'fields': ('quote_number', 'share_token', 'subtotal', 'total', 'created_by')
        }),
    )

    def customer_col(self, obj):
        return obj.customer_display
    customer_col.short_description = _("العميل")

    def total_col(self, obj):
        return format_html('<b style="color:#ec4899; font-size:1rem;">{} {}</b>', f'{obj.total:,.2f}', _cur_sym())
    total_col.short_description = _("الإجمالي")

    def status_badge(self, obj):
        colors = {
            'draft': '#94a3b8', 'sent': '#f59e0b', 'accepted': '#10b981',
            'rejected': '#ef4444', 'expired': '#64748b', 'converted': '#8b5cf6',
        }
        status, label = obj.status, obj.get_status_display()
        # 🐛 [FIX]: الانتهاء كان بيتسجّل بس لما العميل يفتح الرابط — فالقايمة بتقول
        # «في انتظار الرد» لعروض خلصت مدتها من أسابيع.
        if status in ('draft', 'sent') and obj.is_expired:
            status, label = 'expired', dict(PriceQuotation.STATUS_CHOICES)['expired']
        return format_html(
            '<span style="background:{}; color:#fff; padding:3px 10px; border-radius:999px; '
            'font-size:0.78rem; font-weight:700;">{}</span>',
            colors.get(status, '#94a3b8'), label)
    status_badge.short_description = _("الحالة")

    def share_link(self, obj):
        return format_html(
            '<a href="/printing/quotation/view/{}/" target="_blank" rel="noopener" '
            'style="background:linear-gradient(135deg,#ec4899,#8b5cf6); color:#fff; '
            'padding:4px 12px; border-radius:8px; text-decoration:none; font-weight:700; font-size:0.78rem;">'
            '🔗 {}</a>', obj.share_token, gettext('فتح للعميل'))
    share_link.short_description = _("رابط العميل")

    def save_model(self, request, obj, form, change):
        if not obj.pk and not obj.created_by_id:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)

    def save_related(self, request, form, formsets, change):
        # 🐛 [FIX]: تغيير الخصم/الضريبة من غير لمس البنود كان بيسيب الإجمالي القديم.
        super().save_related(request, form, formsets, change)
        form.instance.recalc_totals()


# =====================================================================
# 🖼️ البروفات
# =====================================================================

PROOF_STATUS_COLORS = {'pending': '#f59e0b', 'approved': '#10b981', 'changes_requested': '#ef4444'}


@admin.register(PrintProof)
class PrintProofAdmin(PrintSecureAdmin):
    list_display = ('job', 'version', 'status_badge', 'customer_comment', 'links', 'created_at', 'responded_at')
    list_filter = ('status', 'created_at')
    search_fields = ('job__description', 'job__order__order_number', 'job__order__customer__name')
    list_select_related = ('job__order__customer',)
    readonly_fields = ('version', 'status', 'customer_comment', 'responded_at', 'links')
    actions = ['approve_on_behalf', 'request_changes_on_behalf']

    def save_model(self, request, obj, form, change):
        if not obj.created_by_id:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)

    def status_badge(self, obj):
        return format_html('<span style="background:{};color:white;padding:3px 10px;border-radius:12px;'
                           'font-size:11px;font-weight:700;">{}</span>',
                           PROOF_STATUS_COLORS.get(obj.status, '#64748b'), obj.get_status_display())
    status_badge.short_description = _("الحالة")

    def links(self, obj):
        return _proof_links(obj) if obj and obj.pk else '-'
    links.short_description = _("رابط العميل")

    def _respond(self, request, queryset, action):
        for proof in queryset:
            try:
                proof.respond(action, gettext("سجّلها %(user)s (موافقة بالتليفون/واتساب)") % {'user': request.user.get_username()}
                              if action == 'approve' else "")
                messages.success(request, f"{proof}: {proof.get_status_display()}")
            except ValidationError as exc:
                messages.warning(request, f"{proof}: {'؛ '.join(exc.messages)}")

    @admin.action(description=_("✅ العميل وافق (بالتليفون/واتساب) — ابدأ الطباعة"))
    def approve_on_behalf(self, request, queryset):
        self._respond(request, queryset, 'approve')

    @admin.action(description=_("✏️ العميل طلب تعديل — رجّع المهمة للتصميم"))
    def request_changes_on_behalf(self, request, queryset):
        self._respond(request, queryset, 'reject')


# =====================================================================
# ↩️ الإشعارات الدائنة (مرتجعات/مسموحات المبيعات بعد التسليم)
# =====================================================================


class PrintCreditNoteForm(forms.ModelForm):
    class Meta:
        model = PrintCreditNote
        fields = ('kind', 'order', 'customer', 'date', 'reason', 'amount', 'tax_amount', 'refund_treasury')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if 'order' in self.fields:
            self.fields['order'].queryset = PrintOrder.objects.filter(status='delivered').select_related('customer')
        if 'refund_treasury' in self.fields:
            self.fields['refund_treasury'].queryset = PrintTreasury.objects.filter(
                kind__in=PrintTreasury.MONEY_KINDS)

    def clean(self):
        data = super().clean()
        order, amount, treasury = data.get('order'), data.get('amount'), data.get('refund_treasury')
        customer = data.get('customer')
        if not order and not customer:
            raise ValidationError(_("اختار الطلب أو العميل."))
        if order and customer and order.customer_id != customer.pk:
            raise ValidationError(_("العميل المختار مش هو عميل الطلب."))
        if not order and customer and amount:
            room = customer.account_summary()['balance']
            if amount + (data.get('tax_amount') or Decimal('0')) > room:
                raise ValidationError(_("أكبر من مديونية العميل — المتاح %(a)s.") % {'a': f'{max(room, 0):,.2f}'})
        if order and amount:
            total = amount + (data.get('tax_amount') or Decimal('0'))
            room = order.net_total - order.compute_credited()
            if total > room:
                raise ValidationError(_("المردودات أكبر من قيمة الطلب — المتاح %(a)s.") % {'a': f'{room:,.2f}'})
            if treasury and treasury.balance < total:
                raise ValidationError(gettext("رصيد الخزنة «%(name)s» لا يكفي: المتاح %(available)s.")
                                      % {'name': treasury.name, 'available': f"{treasury.balance:,.2f}"})
        return data


@admin.register(PrintCreditNote)
class PrintCreditNoteAdmin(_SafeSaveDeleteMixin, PrintSecureAdmin):
    form = PrintCreditNoteForm
    list_display = ('number', 'kind', 'order', 'customer', 'date', 'total_display', 'refund_treasury', 'reason')
    list_filter = ('kind', 'date')
    list_select_related = ('order', 'customer', 'refund_treasury')
    search_fields = ('number', 'order__order_number', 'customer__name', 'reason')
    date_hierarchy = 'date'

    def has_change_permission(self, request, obj=None):
        # الإشعار مابيتعدّلش — يتمسح (الفلوس ترجع الخزنة) ويتعمل من جديد.
        return obj is None and super().has_change_permission(request, obj)

    def total_display(self, obj):
        return _amount_html(obj.total, '#dc2626')
    total_display.short_description = _("الإجمالي")
