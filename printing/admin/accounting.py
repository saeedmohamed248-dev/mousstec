"""🏭 لوحة المطابع — الأصول الثابتة وفواتير المصروفات."""
from decimal import Decimal

from django.contrib import admin, messages
from django.utils.html import format_html
from django.utils.translation import gettext, gettext_lazy as _
from django.utils import timezone
from django import forms
from django.core.exceptions import ValidationError

from ..models import (
    PrintTransaction, PrintFixedAsset, PrintExpenseBill,
)
from ._common import PrintSecureAdmin, PrintTransactionForm, _CreatedByFormsetMixin, _SafeSaveDeleteMixin, _amount_html
from .customers import _apply_supplier_credit, _money_txns


# =====================================================================
# 🏭 الأصول الثابتة والإهلاك
# =====================================================================

class PrintFixedAssetForm(forms.ModelForm):
    class Meta:
        model = PrintFixedAsset
        fields = '__all__'

    def clean(self):
        data = super().clean()
        if not self.instance.pk and data.get('funding') == 'cash' and data.get('treasury') and data.get('cost'):
            if data['treasury'].balance < data['cost']:
                raise ValidationError(gettext("رصيد الخزنة «%(name)s» لا يكفي: المتاح %(available)s.")
                                      % {'name': data['treasury'].name, 'available': f"{data['treasury'].balance:,.2f}"})
        return data


@admin.register(PrintFixedAsset)
class PrintFixedAssetAdmin(_SafeSaveDeleteMixin, PrintSecureAdmin):
    form = PrintFixedAssetForm
    list_display = ('name', 'category', 'purchase_date', 'cost_display', 'accumulated_display',
                    'book_value_display', 'status_badge')
    list_filter = ('category', 'funding')
    search_fields = ('name', 'notes')
    fieldsets = (
        (_('🏭 الأصل'), {'fields': ('name', 'category', 'branch', 'machine', 'notes')}),
        (_('💰 الشراء'), {
            'fields': ('purchase_date', 'funding', 'treasury', 'supplier', 'cost'),
            'description': _('نقدي: الثمن بيطلع من الخزنة لوحده. آجل: على حساب المورد. '
                             'افتتاحي: أصل موجود قبل السيستم (اكتب الإهلاك المتراكم عليه).'),
        }),
        (_('📉 الإهلاك (قسط ثابت شهري)'), {
            'fields': ('salvage_value', 'useful_life_months', 'depreciation_start', 'opening_accumulated',
                       'depreciation_summary'),
        }),
        (_('🏷️ البيع / الاستبعاد'), {
            'fields': ('disposed_on', 'disposal_amount', 'disposal_treasury'),
            'description': _('من غير مبلغ = استبعاد (خردة/عطل) — صافي قيمته الدفترية بيتقيّد خسارة.'),
        }),
    )

    def get_readonly_fields(self, request, obj=None):
        ro = ['depreciation_summary']
        if obj and obj.pk:
            ro += ['funding']
            if obj.funding == 'cash':
                ro += ['treasury', 'cost']
            if obj.disposed_on:
                ro += ['disposed_on', 'disposal_amount', 'disposal_treasury']
        return ro

    def depreciation_summary(self, obj):
        if not obj or not obj.pk:
            return '—'
        today = timezone.localdate()
        monthly = obj.depreciation_for_month(today.year, today.month)
        return format_html(
            '{}: <b>{}</b> · {}: <b>{}</b> · {}: <b>{}</b>',
            gettext('القسط الشهري'), f"{monthly:,.2f}",
            gettext('المتراكم'), f"{obj.accumulated_until(today):,.2f}",
            gettext('القيمة الدفترية'), f"{obj.book_value(today):,.2f}")
    depreciation_summary.short_description = _("الإهلاك لحد النهارده")

    def cost_display(self, obj):
        return _amount_html(obj.cost)
    cost_display.short_description = _("التكلفة")

    def accumulated_display(self, obj):
        return _amount_html(obj.accumulated_until(), '#f97316')
    accumulated_display.short_description = _("مجمع الإهلاك")

    def book_value_display(self, obj):
        return _amount_html(obj.book_value() if not obj.disposed_on else 0, '#10b981')
    book_value_display.short_description = _("القيمة الدفترية")

    def status_badge(self, obj):
        if obj.disposed_on:
            return format_html('<span style="color:#ef4444;font-weight:700;">{} <span dir="ltr">{}</span></span>',
                               gettext('اتباع/اتستبعد'), obj.disposed_on)
        return format_html('<span style="color:#10b981;font-weight:700;">{}</span>', gettext('في الخدمة'))
    status_badge.short_description = _("الحالة")


# =====================================================================
# 🧾 فواتير المصروفات والخدمات
# =====================================================================

class BillPaymentInline(admin.TabularInline):
    model = PrintTransaction
    form = PrintTransactionForm
    fk_name = 'bill'
    extra = 0
    fields = ('treasury', 'transaction_type', 'amount', 'withholding_tax', 'description', 'date')
    verbose_name = _("سداد")

    def get_queryset(self, request):
        return _money_txns(super().get_queryset(request))
    verbose_name_plural = _("💸 سداد الفاتورة (سحب = دفعنا للمورد، إيداع = رجّعلنا)")


class PrintExpenseBillForm(forms.ModelForm):
    class Meta:
        model = PrintExpenseBill
        fields = '__all__'

    def clean(self):
        data = super().clean()
        if not self.instance.pk and data.get('payment_terms') == 'cash':
            treasury, amount = data.get('treasury'), data.get('amount')
            total = (amount or Decimal('0')) + (data.get('tax_amount') or Decimal('0'))
            if not treasury:
                raise ValidationError(_("اختار الخزنة اللي هيتصرف منها (أو خلّي الدفع آجل)."))
            if treasury.balance < total:
                raise ValidationError(gettext("رصيد الخزنة «%(name)s» لا يكفي: المتاح %(available)s.")
                                      % {'name': treasury.name, 'available': f"{treasury.balance:,.2f}"})
        return data


@admin.register(PrintExpenseBill)
class PrintExpenseBillAdmin(_CreatedByFormsetMixin, _SafeSaveDeleteMixin, PrintSecureAdmin):
    form = PrintExpenseBillForm
    list_display = ('number', 'supplier', 'date', 'category', 'description', 'total_display', 'paid_amount',
                    'remaining_display', 'status_badge')
    list_filter = ('category', 'is_cancelled', 'payment_terms', 'supplier')
    search_fields = ('number', 'supplier__name', 'supplier_bill_no', 'description')
    list_select_related = ('supplier',)
    date_hierarchy = 'date'
    inlines = [BillPaymentInline]
    actions = ['cancel_selected', 'apply_supplier_credit']

    @admin.action(description=_("💳 سدّد من رصيدنا المقدّم عند المورد"))
    def apply_supplier_credit(self, request, queryset):
        _apply_supplier_credit(request, queryset.order_by('date', 'pk'))
    fieldsets = (
        (_('🧾 الفاتورة'), {
            'fields': ('supplier', 'supplier_bill_no', 'branch', 'date', 'due_date', 'category', 'description'),
            'description': _('التاريخ = الشهر اللي المصروف يخصّه (بيتقيّد فيه حتى لو اتدفع بعدين).'),
        }),
        (_('💰 المبلغ والدفع'), {'fields': ('amount', 'tax_amount', 'payment_terms', 'treasury')}),
    )

    def get_inlines(self, request, obj):
        return [] if obj is None or not obj.pk else super().get_inlines(request, obj)

    def get_readonly_fields(self, request, obj=None):
        if obj and obj.pk:
            ro = ['payment_terms', 'treasury']
            if obj.is_cancelled or obj.paid_amount:
                ro += ['supplier', 'date', 'category', 'amount', 'tax_amount']
            return ro
        return []

    def has_delete_permission(self, request, obj=None):
        if obj is not None and obj.payments.exists():
            return False        # عليها سداد — تتلغي بدل ما تتمسح
        return super().has_delete_permission(request, obj)

    @admin.action(description=_("🚫 الغِ الفواتير المحددة (اللي اتدفع عليها يبقى رصيد لينا عند المورد)"))
    def cancel_selected(self, request, queryset):
        for bill in queryset.filter(is_cancelled=False):
            bill.cancel()
            messages.success(request, gettext("#%(no)s اتلغت.") % {'no': bill.number})

    def total_display(self, obj):
        return _amount_html(obj.total)
    total_display.short_description = _("الإجمالي")

    def remaining_display(self, obj):
        return _amount_html(obj.remaining, '#ef4444' if obj.remaining > 0 else '#10b981')
    remaining_display.short_description = _("الباقي")

    def status_badge(self, obj):
        if obj.is_cancelled:
            label, color = gettext('ملغية'), '#94a3b8'
        elif obj.remaining <= 0:
            label, color = gettext('مدفوعة'), '#10b981'
        else:
            label, color = gettext('مستحقة'), '#f97316'
        return format_html('<span style="color:{};font-weight:700;">{}</span>', color, label)
    status_badge.short_description = _("الحالة")


# =====================================================================
