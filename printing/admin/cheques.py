"""🧾 لوحة المطابع — الشيكات (أوراق القبض والدفع)."""

from django.contrib import admin, messages
from django.urls import reverse
from django.utils.html import format_html
from django.utils.translation import gettext, gettext_lazy as _
from django.utils import timezone
from django import forms
from django.core.exceptions import ValidationError

from ..models import (
    PrintTreasury, MaterialPurchase, PrintExpenseBill,
    PrintCheque,
)
from ._common import PrintSecureAdmin, _SafeSaveDeleteMixin, _amount_html


# 🧾 الشيكات (أوراق قبض ودفع)
# =====================================================================

class PrintChequeForm(forms.ModelForm):
    class Meta:
        model = PrintCheque
        fields = ('direction', 'number', 'bank_name', 'issue_date', 'due_date', 'amount',
                  'customer', 'order', 'supplier', 'purchase', 'bill', 'notes')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if 'purchase' in self.fields:
            self.fields['purchase'].queryset = MaterialPurchase.objects.filter(status='received')
        if 'bill' in self.fields:
            self.fields['bill'].queryset = PrintExpenseBill.objects.filter(is_cancelled=False)


CHEQUE_STATUS_COLORS = {'pending': '#f59e0b', 'cleared': '#10b981', 'bounced': '#ef4444'}


@admin.register(PrintCheque)
class PrintChequeAdmin(_SafeSaveDeleteMixin, PrintSecureAdmin):
    form = PrintChequeForm
    list_display = ('number', 'direction', 'party', 'amount_display', 'issue_date', 'due_badge', 'status_badge',
                    'settle_link')
    list_filter = ('direction', 'status', 'due_date')
    search_fields = ('number', 'bank_name', 'customer__name', 'supplier__name', 'order__order_number')
    list_select_related = ('customer', 'order__customer', 'supplier')
    date_hierarchy = 'due_date'

    def get_readonly_fields(self, request, obj=None):
        if obj and obj.pk:
            return [f for f in PrintChequeForm.Meta.fields if f != 'notes'] + ['status', 'settled_on', 'bounced_on']
        return []

    def has_delete_permission(self, request, obj=None):
        if obj is not None and obj.status != 'pending':
            return False
        return super().has_delete_permission(request, obj)

    def get_urls(self):
        from django.urls import path
        return [path('<int:cheque_id>/settle/', self.admin_site.admin_view(self.settle_view),
                     name='printing_printcheque_settle')] + super().get_urls()

    def settle_view(self, request, cheque_id):
        """صرف الشيك في بنك/خزنة، أو تسجيله مرتجع."""
        from django.shortcuts import get_object_or_404, redirect, render
        cheque = get_object_or_404(PrintCheque, pk=cheque_id)
        if not self.has_change_permission(request, cheque):
            from django.core.exceptions import PermissionDenied
            raise PermissionDenied
        treasuries = PrintTreasury.objects.filter(kind__in=PrintTreasury.MONEY_KINDS, is_active=True)
        if request.method == 'POST' and cheque.status == 'pending':
            from datetime import date as _d
            try:
                on_date = _d.fromisoformat(request.POST.get('date') or '')
            except ValueError:
                on_date = timezone.localdate()
            try:
                if request.POST.get('action') == 'bounce':
                    cheque.bounce(on_date=on_date, user=request.user, reason=request.POST.get('reason', '')[:200])
                    messages.warning(request, gettext("الشيك #%(no)s اتسجّل مرتجع — المديونية رجعت.")
                                     % {'no': cheque.number})
                else:
                    treasury = get_object_or_404(treasuries, pk=request.POST.get('treasury'))
                    cheque.clear(treasury, on_date=on_date, user=request.user)
                    messages.success(request, gettext("الشيك #%(no)s اتصرف في «%(t)s».")
                                     % {'no': cheque.number, 't': treasury.name})
            except ValidationError as exc:
                messages.error(request, '؛ '.join(exc.messages))
            return redirect(reverse('admin:printing_printcheque_changelist'))
        return render(request, 'admin/printing/cheque_settle.html', dict(
            self.admin_site.each_context(request), title=gettext('صرف / رجوع شيك'), cheque=cheque,
            treasuries=treasuries, today=timezone.localdate(), opts=self.model._meta))

    def party(self, obj):
        return obj.party_name
    party.short_description = _("الطرف")

    def amount_display(self, obj):
        return _amount_html(obj.amount, '#10b981' if obj.direction == 'in' else '#ef4444')
    amount_display.short_description = _("المبلغ")

    def due_badge(self, obj):
        late = obj.status == 'pending' and obj.due_date <= timezone.localdate()
        return format_html('<span dir="ltr" style="font-weight:{};color:{};">{}</span>',
                           700 if late else 400, '#ef4444' if late else 'inherit', obj.due_date)
    due_badge.short_description = _("الاستحقاق")
    due_badge.admin_order_field = 'due_date'

    def status_badge(self, obj):
        return format_html('<span style="background:{};color:white;padding:3px 10px;border-radius:12px;'
                           'font-size:11px;font-weight:700;">{}</span>',
                           CHEQUE_STATUS_COLORS.get(obj.status, '#64748b'), obj.get_status_display())
    status_badge.short_description = _("الحالة")

    def settle_link(self, obj):
        if obj.status != 'pending':
            return '—'
        return format_html('<a href="{}">🏦 {}</a>', reverse('admin:printing_printcheque_settle', args=[obj.pk]),
                           gettext('صرف / رجوع'))
    settle_link.short_description = _("إجراء")


# =====================================================================
