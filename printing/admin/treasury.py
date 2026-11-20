"""💰 لوحة المطابع — الخزائن والحركات والتحويلات ومطابقة البنك وإقرار الضريبة."""

from django.contrib import admin, messages
from django.urls import reverse
from django.utils.html import format_html
from django.utils.translation import gettext, gettext_lazy as _
from erp_core.localization import current_tenant_symbol as _cur_sym
from django.db import models
from django import forms
from django.core.exceptions import ValidationError

from ..models import (
    PrintOrder, PrintTreasury, PrintTransaction, PrintTreasuryTransfer, PrintBankReconciliation,
    PrintVatReturn,
)
from ._common import PrintSecureAdmin, PrintTransactionForm, _SafeSaveDeleteMixin, _amount_html


# =====================================================================
# 💰 الخزينة
# =====================================================================

class PrintTransactionInline(admin.TabularInline):
    model = PrintTransaction
    form = PrintTransactionForm
    extra = 1
    fields = ('transaction_type', 'category', 'amount', 'description', 'date')


@admin.register(PrintTreasury)
class PrintTreasuryAdmin(PrintSecureAdmin):
    list_display = ('name', 'kind', 'branch', 'balance_display', 'is_active', 'reconcile_link')
    list_filter = ('kind', 'is_active')
    inlines = [PrintTransactionInline]

    def reconcile_link(self, obj):
        if obj.kind not in ('bank', 'wallet'):
            return '—'
        return format_html('<a href="/printing/accounting/reconcile/{}/">🏦 {}</a>', obj.pk, gettext('مطابقة مع الكشف'))
    reconcile_link.short_description = _("مطابقة البنك")

    def get_inlines(self, request, obj):
        # حركات الحوافظ الافتراضية بتيجي من الشيكات والخصم من المنبع — مابتتعدّلش من هنا.
        if obj is not None and not obj.is_money:
            return []
        return super().get_inlines(request, obj)

    def has_delete_permission(self, request, obj=None):
        if obj is not None and not obj.is_money:
            return False
        return super().has_delete_permission(request, obj)

    def get_readonly_fields(self, request, obj=None):
        # The balance is the running sum of the transactions below. Typing a new
        # number over it silently broke that — adjust with a deposit/withdrawal.
        # (Still editable on create, as the opening balance.)
        ro = list(super().get_readonly_fields(request, obj))
        if obj and obj.pk:
            ro.append('balance')
        return ro

    def save_formset(self, request, form, formset, change):
        instances = formset.save(commit=False)
        for inst in instances:
            if not inst.created_by_id:
                inst.created_by = request.user
            inst.save()
        for obj in formset.deleted_objects:
            try:
                obj.delete()
            except ValidationError as exc:      # رجل تحويل بين خزنتين
                messages.error(request, '؛ '.join(exc.messages))
        formset.save_m2m()

    def balance_display(self, obj):
        color = '#10b981' if obj.balance >= 0 else '#ef4444'
        return format_html('<b style="color:{};">{} {}</b>', color, f"{float(obj.balance):,.2f}", _cur_sym())
    balance_display.short_description = _("الرصيد")


# 🧭 شاشات بسيطة لكل حركة فلوس بدل فورم «حركة مالية» العام (15 خانة بيلخبطوا صاحب
#    المطبعة): /printing/printtransaction/add/?mode=expense … كل شاشة فيها خاناتها بس،
#    والنوع والبند بيتحددوا منها — والقيد في الدفاتر هو هو (ledger.sync_txn).
TXN_MODES = {
    'expense': {
        'type': 'out', 'title': _("💸 مصروف جديد"),
        'help': _("فلوس طلعت من الخزنة دلوقتي على مصروف (إيجار، كهرباء، مواصلات…). "
                  "لو الفاتورة لسه ماتدفعتش سجّلها «فاتورة مصروف آجلة»."),
        'categories': ('rent', 'utilities', 'maintenance', 'shipping', 'marketing', 'materials',
                       'salaries', 'other_expense'),
        'fields': ('amount', 'category', 'treasury', 'date', 'description'),
    },
    'income': {
        'type': 'in', 'title': _("💰 إيراد آخر / إيداع"),
        'help': _("فلوس دخلت الخزنة من برا الطلبات: رأس مال من المالك أو إيراد عارض. "
                  "فلوس العملاء بتتسجّل من «تحصيل من عميل»."),
        'categories': ('other_income', 'capital'),
        'fields': ('amount', 'category', 'treasury', 'date', 'description'),
    },
    'collect': {
        'type': 'in', 'category': 'customer', 'title': _("💵 تحصيل من عميل"),
        'help': _("اختار الطلب اللي بيدفع عليه (الطلبات اللي لسه عليها فلوس بس)، "
                  "أو العميل لوحده لو دفعة على حسابه."),
        'fields': ('customer', 'order', 'amount', 'withholding_tax', 'treasury', 'date', 'description'),
    },
    'pay_supplier': {
        'type': 'out', 'category': 'supplier', 'title': _("🏭 سداد لمورد"),
        'help': _("اختار فاتورة الشراء أو فاتورة المصروف اللي بتسدّدها، أو المورد لوحده لو دفعة على حسابه."),
        'fields': ('supplier', 'purchase', 'bill', 'amount', 'withholding_tax', 'treasury', 'date', 'description'),
    },
}


@admin.register(PrintTransaction)
class PrintTransactionAdmin(PrintSecureAdmin):
    form = PrintTransactionForm
    list_display = ('type_badge', 'amount_display', 'category', 'treasury', 'party', 'description', 'date')
    list_filter = ('transaction_type', 'category', 'treasury', 'date')
    search_fields = ('description', 'order__order_number', 'customer__name', 'supplier__name')
    list_select_related = ('treasury', 'order', 'customer', 'supplier', 'purchase')
    exclude = ('created_by',)
    readonly_fields = ('fixed_asset',)

    def has_change_permission(self, request, obj=None):
        # حركات الحوافظ الافتراضية (شيك / خصم من المنبع) بتتظبط من مستندها.
        if obj is not None and not obj.treasury.is_money:
            return False
        return super().has_change_permission(request, obj)

    @staticmethod
    def _mode(request, obj=None):
        mode = request.GET.get('mode', '')
        return mode if obj is None and mode in TXN_MODES else ''

    def get_fields(self, request, obj=None):
        mode = self._mode(request, obj)
        return list(TXN_MODES[mode]['fields']) if mode else super().get_fields(request, obj)

    def get_fieldsets(self, request, obj=None):
        mode = self._mode(request, obj)
        if not mode:
            return super().get_fieldsets(request, obj)
        cfg = TXN_MODES[mode]
        return [(cfg['title'], {'fields': cfg['fields'], 'description': cfg['help']})]

    def get_form(self, request, obj=None, change=False, **kwargs):
        form = super().get_form(request, obj, change=change, **kwargs)
        mode = self._mode(request, obj)
        if not mode:
            return form
        cfg = TXN_MODES[mode]

        class GuidedForm(form):
            def __init__(self, *args, **kw):
                super().__init__(*args, **kw)
                self.instance.transaction_type = cfg['type']
                if cfg.get('category'):
                    self.instance.category = cfg['category']
                if 'category' in self.fields:
                    field = self.fields['category']
                    # من غير اختيار مسبق — أول بند («رواتب») كان بيتختار لوحده
                    field.choices = [('', gettext("— اختار البند —"))] + [
                        c for c in field.choices if c[0] in cfg['categories']]
                    field.required = True
                    field.help_text = ''
                if 'order' in self.fields:      # الطلبات اللي لسه عليها فلوس بس
                    self.fields['order'].queryset = (
                        PrintOrder.objects.exclude(status='cancelled')
                        .annotate(_due=models.F('total_amount') - models.F('discount') + models.F('tax_amount')
                                  - models.F('credited_amount') - models.F('paid_amount'))
                        .filter(_due__gt=0).select_related('customer').order_by('-date_created'))
                treasury = self.fields.get('treasury')
                if treasury is not None and treasury.queryset.count() == 1:
                    treasury.initial = treasury.queryset.first().pk

        return GuidedForm

    def add_view(self, request, form_url='', extra_context=None):
        mode = self._mode(request)
        if mode:
            extra_context = {**(extra_context or {}), 'title': TXN_MODES[mode]['title']}
        return super().add_view(request, form_url, extra_context)

    def response_add(self, request, obj, post_url_continue=None):
        response = super().response_add(request, obj, post_url_continue)
        mode = self._mode(request)
        if mode and '_addanother' in request.POST:
            response['Location'] = f"{request.path}?mode={mode}"
        elif mode and '_continue' not in request.POST:
            response['Location'] = reverse('admin:index')   # رجوع للوحة بعد ما يسجّل
        return response

    def delete_model(self, request, obj):
        try:
            super().delete_model(request, obj)
        except ValidationError as exc:      # رجل تحويل / شيك / خصم من المنبع
            messages.error(request, '؛ '.join(exc.messages))

    def delete_queryset(self, request, queryset):
        for obj in queryset:
            self.delete_model(request, obj)

    def party(self, obj):
        if obj.order_id:
            return f"#{obj.order.order_number}"
        return obj.customer or obj.supplier or '-'
    party.short_description = _("مرتبطة بـ")


    def save_model(self, request, obj, form, change):
        if not obj.created_by_id:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)

    def type_badge(self, obj):
        if obj.transaction_type == 'in':
            return format_html('<span style="color:#10b981;font-weight:bold;">🟢 {}</span>', gettext('إيداع'))
        return format_html('<span style="color:#ef4444;font-weight:bold;">🔴 {}</span>', gettext('مصروف'))
    type_badge.short_description = _("النوع")

    def amount_display(self, obj):
        color = '#10b981' if obj.transaction_type == 'in' else '#ef4444'
        return format_html('<b style="color:{};">{} {}</b>', color, f"{float(obj.amount):,.2f}", _cur_sym())
    amount_display.short_description = _("المبلغ")


# 🔁 التحويل بين الخزائن + 🏦 مطابقات البنوك + 🧾 إقرارات الضريبة
# =====================================================================

class PrintTreasuryTransferForm(forms.ModelForm):
    class Meta:
        model = PrintTreasuryTransfer
        fields = ('from_treasury', 'to_treasury', 'amount', 'date', 'notes')

    def clean(self):
        data = super().clean()
        src, dst, amount = data.get('from_treasury'), data.get('to_treasury'), data.get('amount')
        if src and dst and src == dst:
            raise ValidationError(_("اختار خزنتين مختلفتين."))
        if any(t and t.kind in ('cheques_in', 'cheques_out') for t in (src, dst)):
            # التحويل اليدوي كان بيفضّي الحافظة والشيك يفضل «لسه ماتصرفش».
            raise ValidationError(_("صرف الشيكات بيتسجّل من شاشة «الشيكات» (صرف / رجوع)."))
        if src and amount and src.balance < amount:
            raise ValidationError(gettext("رصيد الخزنة «%(name)s» لا يكفي: المتاح %(available)s.")
                                  % {'name': src.name, 'available': f"{src.balance:,.2f}"})
        return data


@admin.register(PrintTreasuryTransfer)
class PrintTreasuryTransferAdmin(_SafeSaveDeleteMixin, PrintSecureAdmin):
    form = PrintTreasuryTransferForm
    list_display = ('date', 'from_treasury', 'to_treasury', 'amount_display', 'notes', 'created_by')
    list_filter = ('from_treasury', 'to_treasury')
    list_select_related = ('from_treasury', 'to_treasury', 'created_by')
    date_hierarchy = 'date'

    def has_change_permission(self, request, obj=None):
        return obj is None and super().has_change_permission(request, obj)

    def amount_display(self, obj):
        return _amount_html(obj.amount)
    amount_display.short_description = _("المبلغ")


@admin.register(PrintBankReconciliation)
class PrintBankReconciliationAdmin(PrintSecureAdmin):
    list_display = ('treasury', 'statement_date', 'statement_balance', 'cleared_balance', 'difference_display',
                    'notes', 'created_by')
    list_filter = ('treasury',)
    list_select_related = ('treasury', 'created_by')

    def has_add_permission(self, request):
        return False        # المطابقة من صفحتها (بتعلّم الحركات)

    def has_change_permission(self, request, obj=None):
        return False

    def difference_display(self, obj):
        return _amount_html(obj.difference, '#10b981' if not obj.difference else '#ef4444')
    difference_display.short_description = _("الفرق")


@admin.register(PrintVatReturn)
class PrintVatReturnAdmin(PrintSecureAdmin):
    list_display = ('date_from', 'date_to', 'output_vat', 'input_vat', 'net_display', 'notes', 'created_by')
    list_select_related = ('created_by',)

    def has_add_permission(self, request):
        return False        # الإقرار من تبويب «ضريبة القيمة المضافة» في الدفاتر

    def has_change_permission(self, request, obj=None):
        return False

    def net_display(self, obj):
        return _amount_html(obj.net_payable, '#ef4444' if obj.net_payable > 0 else '#10b981')
    net_display.short_description = _("الصافي (موجب = مستحق للضرايب)")


# =====================================================================
