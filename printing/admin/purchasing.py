"""🚚 لوحة المطابع — الموردين والمشتريات ومرتجعاتها والتسديد من الرصيد المقدّم."""

from django.contrib import admin, messages
from django.urls import reverse
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from django.utils.translation import gettext, gettext_lazy as _
from erp_core.localization import current_tenant_symbol as _cur_sym
from django import forms
from django.core.exceptions import ValidationError

from ..models import (
    PrintTransaction, PrintSupplier, MaterialPurchase, MaterialPurchaseLine, MaterialPurchaseReturn, MaterialPurchaseReturnLine, PrintSupplierAllocation,
)
from ._common import PrintSecureAdmin, PrintTransactionForm, _CreatedByFormsetMixin, _SafeSaveDeleteMixin, _amount_html, _money
from .customers import _apply_supplier_credit, _money_txns
from .production import PrintChequeInline


# =====================================================================
# 🚚 الموردين ومشتريات الخامات
# =====================================================================

class SupplierPaymentInline(admin.TabularInline):
    """💸 دفعات على حساب المورد (من غير فاتورة) — دفعة مقدمة أو سداد رصيد قديم."""
    model = PrintTransaction
    form = PrintTransactionForm
    fk_name = 'supplier'
    extra = 0
    fields = ('treasury', 'transaction_type', 'amount', 'withholding_tax', 'description', 'date')
    verbose_name = _("دفعة على حساب المورد")
    verbose_name_plural = _("💸 دفعات على حساب المورد (سحب = دفعنا له، إيداع = رجّعلنا فلوس)")

    def get_queryset(self, request):
        return _money_txns(super().get_queryset(request).filter(purchase__isnull=True, bill__isnull=True))


@admin.register(PrintSupplier)
class PrintSupplierAdmin(_CreatedByFormsetMixin, PrintSecureAdmin):
    list_display = ('name', 'company', 'phone', 'balance_display', 'is_active', 'statement_link')
    list_filter = ('is_active',)
    search_fields = ('name', 'company', 'phone')
    inlines = [SupplierPaymentInline]
    actions = ['settle_docs_from_credit']

    @admin.action(description=_("💳 سدّد الفواتير المفتوحة من رصيدنا المقدّم"))
    def settle_docs_from_credit(self, request, queryset):
        docs = []
        for supplier in queryset:
            docs += list(supplier.purchases.filter(status='received').order_by('date', 'pk'))
            docs += list(supplier.bills.filter(is_cancelled=False).order_by('date', 'pk'))
        _apply_supplier_credit(request, docs)

    def balance_display(self, obj):
        bal = obj.balance
        color = '#ef4444' if bal > 0 else '#10b981'
        label = gettext('علينا') if bal > 0 else (gettext('لينا') if bal < 0 else '')
        return format_html('<b style="color:{};">{} {}</b> <small>{}</small>', color,
                           f"{float(abs(bal)):,.2f}", _cur_sym(), label)
    balance_display.short_description = _("الرصيد")

    def statement_link(self, obj):
        return format_html(
            '<a href="/printing/supplier/{}/statement/" target="_blank" '
            'style="background:linear-gradient(135deg,#0ea5e9,#8b5cf6); color:#fff; '
            'padding:5px 12px; border-radius:8px; text-decoration:none; font-weight:700; font-size:0.82rem;">'
            '📒 {}</a>', obj.pk, gettext('كشف حساب'))
    statement_link.short_description = _("كشف حساب")


class MaterialPurchaseForm(forms.ModelForm):
    receive_now = forms.BooleanField(
        required=False, label=_("📥 استلم البضاعة دلوقتي"),
        help_text=_("الخامات تدخل المخزن بمتوسط التكلفة، ولو الدفع نقدي المبلغ يتخصم من الخزنة لوحده."))

    class Meta:
        model = MaterialPurchase
        fields = '__all__'

    def clean(self):
        data = super().clean()
        if data.get('receive_now') and data.get('payment_terms') == 'cash' and not data.get('treasury'):
            raise ValidationError(_("اختار الخزنة اللي هيتصرف منها (أو خلّي الدفع آجل)."))
        return data


class MaterialPurchaseLineInline(admin.TabularInline):
    model = MaterialPurchaseLine
    extra = 1
    fields = ('material', 'quantity', 'unit_cost', 'line_total')
    readonly_fields = ('line_total',)
    verbose_name = _("بند خامة")
    verbose_name_plural = _("📦 بنود الفاتورة (الخامات)")

    def _editable(self, obj):
        return obj is None or obj.status == 'draft'

    def has_add_permission(self, request, obj=None):
        return self._editable(obj) and super().has_add_permission(request, obj)

    def has_change_permission(self, request, obj=None):
        return self._editable(obj) and super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        return self._editable(obj) and super().has_delete_permission(request, obj)


class PurchasePaymentInline(admin.TabularInline):
    """💸 مدفوعات الفاتورة — في النقدي بتتسجّل لوحدها وقت الاستلام."""
    model = PrintTransaction
    form = PrintTransactionForm
    fk_name = 'purchase'
    extra = 0
    fields = ('treasury', 'transaction_type', 'amount', 'withholding_tax', 'description', 'date')
    verbose_name = _("دفعة")

    def get_queryset(self, request):
        return _money_txns(super().get_queryset(request))
    verbose_name_plural = _("💸 مدفوعات الفاتورة (سحب = دفعنا للمورد، إيداع = استرداد منه)")


PURCHASE_STATUS_COLORS = {'draft': '#94a3b8', 'received': '#10b981', 'cancelled': '#ef4444'}


@admin.register(MaterialPurchase)
class MaterialPurchaseAdmin(_CreatedByFormsetMixin, PrintSecureAdmin):
    form = MaterialPurchaseForm
    list_display = ('number', 'supplier', 'date', 'status_badge', 'payment_terms', 'net_display',
                    'paid_display', 'remaining_display')
    list_filter = ('status', 'payment_terms', 'supplier', 'date')
    search_fields = ('number', 'supplier__name', 'supplier_invoice_no')
    list_select_related = ('supplier',)
    date_hierarchy = 'date'
    inlines = [MaterialPurchaseLineInline, PurchasePaymentInline, PrintChequeInline]
    actions = ['receive_selected', 'cancel_selected', 'apply_supplier_credit']

    def get_inlines(self, request, obj):
        # الشيكات بتتعرض على فاتورة محفوظة بس (مش في «فاتورة جديدة»).
        inlines = list(super().get_inlines(request, obj))
        if obj is None or not obj.pk:
            inlines.remove(PrintChequeInline)
        return inlines

    @admin.action(description=_("💳 سدّد من رصيدنا المقدّم عند المورد"))
    def apply_supplier_credit(self, request, queryset):
        _apply_supplier_credit(request, queryset.order_by('date', 'pk'))
    fieldsets = (
        (_('🧾 بيانات الفاتورة'), {
            'fields': ('supplier', 'supplier_invoice_no', 'branch', 'date', 'notes'),
        }),
        (_('💰 الدفع والإجمالي'), {
            'fields': ('payment_terms', 'treasury', 'discount', 'tax_amount', 'totals', 'receive_now'),
            'description': _('نقدي: الفلوس بتطلع من الخزنة لوحدها وقت الاستلام. آجل: المبلغ بيفضل على حساب المورد.'),
        }),
    )

    def get_readonly_fields(self, request, obj=None):
        ro = ['totals', 'returns_info']
        if obj and obj.status != 'draft':
            ro += ['supplier', 'branch', 'date', 'payment_terms', 'treasury', 'discount', 'tax_amount']
        return ro

    def get_fieldsets(self, request, obj=None):
        fieldsets = super().get_fieldsets(request, obj)
        if obj and obj.status != 'draft':
            fieldsets = [(name, dict(opts, fields=tuple(f for f in opts['fields'] if f != 'receive_now')))
                         for name, opts in fieldsets]
        if obj and obj.status == 'received':
            fieldsets = list(fieldsets) + [(_('📤 المرتجعات'), {'fields': ('returns_info',)})]
        return fieldsets

    def returns_info(self, obj):
        """المرتجعات على الفاتورة + زرار «مرتجع جديد» (جزئي، بالكمية اللي لسه في المخزن)."""
        if not obj or not obj.pk:
            return '—'
        rows = [format_html('<a href="{}">#{}</a> ({}) — {}', reverse('admin:printing_materialpurchasereturn_change',
                                                                     args=[r.pk]),
                            r.number, r.get_status_display(), f"{r.total:,.2f}")
                for r in obj.returns.all()]
        new = format_html('<a href="{}?purchase={}" style="display:inline-block;padding:6px 12px;border-radius:8px;'
                          'background:#f97316;color:#fff;font-weight:700;text-decoration:none;">📤 {}</a>',
                          reverse('admin:printing_materialpurchasereturn_add'), obj.pk, gettext('مرتجع جديد للمورد'))
        return format_html('{}<div style="margin-top:6px;">{}</div>',
                           format_html(''.join(['{}<br>'] * len(rows)), *rows) if rows else '', new)
    returns_info.short_description = _("المرتجعات")

    def has_delete_permission(self, request, obj=None):
        if obj is not None and obj.status == 'received':
            return False
        return super().has_delete_permission(request, obj)

    def delete_queryset(self, request, queryset):
        received = queryset.filter(status='received')
        if received.exists():
            messages.warning(request, gettext("الفواتير المستلمة مابتتمسحش — الغيها الأول: ")
                             + ', '.join(received.values_list('number', flat=True)))
        super().delete_queryset(request, queryset.exclude(status='received'))

    def totals(self, obj):
        if not obj or not obj.pk:
            return '-'
        return format_html('{}{}', format_html(
            '{} {} − {} {} + {} {} = <b>{}</b> · {} {} · {} <b>{}</b> · {} <b>{}</b>',
            gettext('البنود'), f"{obj.total_amount:,.2f}", gettext('خصم'), f"{obj.discount:,.2f}",
            gettext('ضريبة'), f"{obj.tax_amount:,.2f}", f"{obj.net_total:,.2f}",
            gettext('المدفوع'), f"{obj.paid_amount:,.2f}", gettext('الباقي'), f"{obj.remaining:,.2f}",
            gettext('الحالة:'), obj.get_status_display()), self._pay_rest_link(obj))
    totals.short_description = _("الإجمالي")

    @staticmethod
    def _pay_rest_link(obj):
        """💵 «سداد الباقي» على فاتورة مستلمة لسه عليها فلوس — من غيره كان لازم تروح «حركة مالية» وتدوّر عليها."""
        if obj.status != 'received' or obj.remaining <= 0:
            return mark_safe('')
        return format_html(
            ' <a href="{}?supplier={}&amp;doc=purchase:{}" style="display:inline-block;margin-inline-start:8px;padding:4px 12px;'
            'border-radius:8px;background:#1E2A57;color:#fff;font-weight:700;text-decoration:none;">💵 {}</a>',
            reverse('printing:quick_pay_supplier'), obj.supplier_id, obj.pk, gettext('سداد الباقي'))

    def save_model(self, request, obj, form, change):
        if not obj.pk and not obj.created_by_id:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        purchase = form.instance
        purchase.recalc_total()
        if form.cleaned_data.get('receive_now') and purchase.status == 'draft':
            self._receive(request, purchase)

    def _receive(self, request, purchase):
        try:
            purchase.receive(user=request.user)
        except ValidationError as exc:
            messages.error(request, f"#{purchase.number}: {'؛ '.join(exc.messages)}")
            return False
        msg = (gettext("#%(no)s: الخامات دخلت المخزن واتدفعت من الخزنة.") if purchase.payment_terms == 'cash'
               else gettext("#%(no)s: الخامات دخلت المخزن وعلى حساب المورد (آجل)."))
        messages.success(request, msg % {'no': purchase.number})
        return True

    @admin.action(description=_("📥 استلم الفواتير المحددة (تدخل المخزن وتتدفع لو نقدي)"))
    def receive_selected(self, request, queryset):
        for purchase in queryset.filter(status='draft'):
            self._receive(request, purchase)

    @admin.action(description=_("↩️ الغي الفواتير المحددة (الخامات ترجع للمورد)"))
    def cancel_selected(self, request, queryset):
        for purchase in queryset.exclude(status='cancelled'):
            try:
                purchase.cancel(user=request.user)
            except ValidationError as exc:
                messages.error(request, f"#{purchase.number}: {'؛ '.join(exc.messages)}")
                continue
            note = (" — " + gettext("المدفوع %(amount)s بقى رصيد لينا عند المورد") % {'amount': f"{purchase.paid_amount:,.2f}"}
                    if purchase.paid_amount > 0 else '')
            messages.success(request, gettext("#%(no)s اتلغت") % {'no': purchase.number} + f"{note}.")

    def status_badge(self, obj):
        return format_html('<span style="background:{};color:white;padding:3px 10px;border-radius:12px;'
                           'font-size:11px;font-weight:700;">{}</span>',
                           PURCHASE_STATUS_COLORS.get(obj.status, '#64748b'), obj.get_status_display())
    status_badge.short_description = _("الحالة")

    def net_display(self, obj):
        return _money(obj.net_total)
    net_display.short_description = _("الإجمالي")

    def paid_display(self, obj):
        return _money(obj.paid_amount)
    paid_display.short_description = _("المدفوع")

    def remaining_display(self, obj):
        r = obj.remaining
        color = '#ef4444' if r > 0 else '#10b981'
        return format_html('<b style="color:{};">{}</b> {}', color, f"{float(r):,.2f}", _cur_sym())
    remaining_display.short_description = _("الباقي")


# 📤 مرتجعات المشتريات
# =====================================================================

class MaterialPurchaseReturnLineInline(admin.TabularInline):
    model = MaterialPurchaseReturnLine
    extra = 1
    fields = ('purchase_line', 'quantity', 'value')
    readonly_fields = ('value',)
    verbose_name = _("بند مرتجع")
    verbose_name_plural = _("📦 الخامات المرتجعة")

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == 'purchase_line':
            ret_id = (request.resolver_match.kwargs.get('object_id') if request.resolver_match else None)
            purchase_id = (MaterialPurchaseReturn.objects.filter(pk=ret_id).values_list('purchase_id', flat=True)
                           .first() if ret_id else None)
            kwargs['queryset'] = MaterialPurchaseLine.objects.filter(purchase_id=purchase_id).select_related('material')
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def _editable(self, obj):
        return obj is None or obj.status == 'draft'

    def has_add_permission(self, request, obj=None):
        return self._editable(obj) and super().has_add_permission(request, obj)

    def has_change_permission(self, request, obj=None):
        return self._editable(obj) and super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        return self._editable(obj) and super().has_delete_permission(request, obj)


class MaterialPurchaseReturnForm(forms.ModelForm):
    post_now = forms.BooleanField(
        required=False, label=_("📤 رحّل المرتجع دلوقتي"),
        help_text=_("الخامات تخرج من المخزن بتكلفة الفاتورة، وقيمتها تنزل من المستحق للمورد (أو تترد في الخزنة)."))

    class Meta:
        model = MaterialPurchaseReturn
        fields = ('purchase', 'date', 'reason', 'refund_treasury')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if 'purchase' in self.fields:
            self.fields['purchase'].queryset = MaterialPurchase.objects.filter(status='received').select_related('supplier')


@admin.register(MaterialPurchaseReturn)
class MaterialPurchaseReturnAdmin(_CreatedByFormsetMixin, _SafeSaveDeleteMixin, PrintSecureAdmin):
    form = MaterialPurchaseReturnForm
    list_display = ('number', 'purchase', 'supplier', 'date', 'status', 'total_display', 'refund_treasury')
    list_filter = ('status', 'date')
    search_fields = ('number', 'purchase__number', 'purchase__supplier__name', 'reason')
    list_select_related = ('purchase__supplier', 'refund_treasury')
    inlines = [MaterialPurchaseReturnLineInline]
    actions = ['post_selected']

    def get_inlines(self, request, obj):
        # البنود بتتختار من بنود الفاتورة — بعد ما المرتجع يتحفظ على فاتورة.
        return [] if obj is None or not obj.pk else super().get_inlines(request, obj)

    def get_fields(self, request, obj=None):
        fields = ['purchase', 'date', 'reason', 'refund_treasury']
        if obj and obj.pk:
            fields += ['status', 'value', 'tax_amount']
            if obj.status == 'draft':
                fields.append('post_now')
        return fields

    def get_readonly_fields(self, request, obj=None):
        if obj and obj.pk:
            ro = ['purchase', 'status', 'value', 'tax_amount']
            if obj.status != 'draft':
                ro += ['date', 'reason', 'refund_treasury']
            return ro
        return []

    def has_delete_permission(self, request, obj=None):
        if obj is not None and obj.status == 'posted':
            return False
        return super().has_delete_permission(request, obj)

    def response_add(self, request, obj, post_url_continue=None):
        messages.info(request, gettext("اختار الخامات والكميات المرتجعة وبعدين علّم «رحّل المرتجع دلوقتي»."))
        from django.http import HttpResponseRedirect
        return HttpResponseRedirect(reverse('admin:printing_materialpurchasereturn_change', args=[obj.pk]))

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        ret = form.instance
        if form.cleaned_data.get('post_now') and ret.status == 'draft':
            self._post(request, ret)

    def _post(self, request, ret):
        try:
            ret.post(user=request.user)
        except ValidationError as exc:
            messages.error(request, f"#{ret.number}: {'؛ '.join(exc.messages)}")
            return
        messages.success(request, gettext("#%(no)s اترحّل: خرج من المخزن بقيمة %(v)s.") % {
            'no': ret.number, 'v': f"{ret.total:,.2f}"})

    @admin.action(description=_("📤 رحّل المرتجعات المحددة"))
    def post_selected(self, request, queryset):
        for ret in queryset.filter(status='draft'):
            self._post(request, ret)

    def supplier(self, obj):
        return obj.purchase.supplier
    supplier.short_description = _("المورد")

    def total_display(self, obj):
        return _amount_html(obj.total)
    total_display.short_description = _("القيمة")


# =====================================================================


# 💳 التسديد من رصيدنا المقدّم عند المورد
# =====================================================================

@admin.register(PrintSupplierAllocation)
class PrintSupplierAllocationAdmin(_SafeSaveDeleteMixin, PrintSecureAdmin):
    list_display = ('date', 'supplier', 'purchase', 'bill', 'amount', 'notes', 'created_by')
    list_filter = ('supplier',)
    list_select_related = ('supplier', 'purchase', 'bill', 'created_by')
    fields = ('purchase', 'bill', 'amount', 'date', 'notes')

    def has_change_permission(self, request, obj=None):
        return obj is None and super().has_change_permission(request, obj)
