"""🏢 لوحة المطابع — الفروع والعملاء."""
from decimal import Decimal

from django.contrib import admin, messages
from django.utils.html import format_html
from django.utils.translation import gettext, gettext_lazy as _
from erp_core.localization import current_tenant_symbol as _cur_sym
from django.core.exceptions import ValidationError

from ..models import (
    PrintBranch, PrintCustomer, PrintOrder, PrintTreasury, PrintTransaction, PrintCreditAllocation,
)
from ._common import PrintSecureAdmin, PrintTransactionForm, _CreatedByFormsetMixin, _money


# =====================================================================
# 🏢 الفروع والعملاء
# =====================================================================

@admin.register(PrintBranch)
class PrintBranchAdmin(PrintSecureAdmin):
    list_display = ('name', 'phone', 'is_active')
    search_fields = ('name',)


def _apply_supplier_credit(request, docs):
    """سدّد الفواتير من رصيدنا المقدّم عند المورد (الأقدم الأول)."""
    from ..models import PrintSupplierAllocation
    total = Decimal('0')
    for doc in docs:
        try:
            total += PrintSupplierAllocation.apply_available(doc, user=request.user)
        except ValidationError as exc:
            messages.error(request, f"#{doc.number}: {'؛ '.join(exc.messages)}")
    messages.success(request, gettext("اتسدّد %(amount)s من رصيدنا عند الموردين.") % {'amount': f"{total:,.2f}"})


def _money_txns(qs):
    """الشيكات والخصم من المنبع بيظهروا في شاشاتهم (أو كعمود في الدفعة) — مش كسطور تتعدّل."""
    return qs.filter(treasury__kind__in=PrintTreasury.MONEY_KINDS)


class OnAccountPaymentInline(admin.TabularInline):
    """💳 دفعات على حساب العميل نفسه (من غير طلب) = رصيد مقدّم يتسدّد منه أي طلب بعدين."""
    model = PrintTransaction
    form = PrintTransactionForm
    fk_name = 'customer'
    extra = 0
    fields = ('treasury', 'transaction_type', 'amount', 'withholding_tax', 'description', 'date')
    verbose_name = _("دفعة على الحساب")
    verbose_name_plural = _("💳 دفعات على حساب العميل (رصيد مقدّم — إيداع = دفعة، سحب = رد فلوس له)")

    def get_queryset(self, request):
        return _money_txns(super().get_queryset(request).filter(order__isnull=True))


@admin.register(PrintCustomer)
class PrintCustomerAdmin(_CreatedByFormsetMixin, PrintSecureAdmin):
    list_display = ('name', 'company', 'phone', 'whatsapp', 'orders_count', 'balance_display',
                    'credit_display', 'statement_link')
    search_fields = ('name', 'company', 'phone')
    list_filter = ('created_at',)
    inlines = [OnAccountPaymentInline]
    actions = ['settle_open_orders_from_credit']

    def balance_display(self, obj):
        bal = obj.account_summary()['balance']
        color = '#ef4444' if bal > 0 else '#10b981'
        label = gettext('عليه') if bal > 0 else (gettext('ليه') if bal < 0 else '')
        return format_html('<b style="color:{};">{} {}</b> <small>{}</small>', color,
                           f"{float(abs(bal)):,.2f}", _cur_sym(), label)
    balance_display.short_description = _("الرصيد")

    def credit_display(self, obj):
        credit = obj.account_summary()['credit']
        return _money(credit) if credit else '-'
    credit_display.short_description = _("رصيد مقدّم متاح")

    @admin.action(description=_("💳 سدّد الطلبات المفتوحة من الرصيد المقدّم"))
    def settle_open_orders_from_credit(self, request, queryset):
        total = 0
        for customer in queryset:
            for order in customer.printorder_set.exclude(status__in=PrintOrder.NON_DEBT_STATUSES).order_by('date_created'):
                try:
                    total += PrintCreditAllocation.apply_available(order, user=request.user)
                except ValidationError as exc:
                    messages.error(request, f"#{order.order_number}: {'؛ '.join(exc.messages)}")
        messages.success(request, gettext("اتسدّد %(amount)s من الأرصدة المقدّمة.") % {'amount': f"{total:,.2f}"})

    def orders_count(self, obj):
        count = obj.printorder_set.count()
        return format_html('<b>{}</b>', count)
    orders_count.short_description = _("عدد الطلبات")

    def statement_link(self, obj):
        return format_html(
            '<a href="/printing/customer/{}/statement/" target="_blank" '
            'style="background:linear-gradient(135deg,#ec4899,#8b5cf6); color:#fff; '
            'padding:5px 12px; border-radius:8px; text-decoration:none; font-weight:700; font-size:0.82rem;">'
            '📒 {}</a>', obj.pk, gettext('كشف حساب'))
    statement_link.short_description = _("كشف حساب")
