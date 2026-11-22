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


def _site_root():
    """https://<دومين الشركة> — الأدمن instance واحد لكل الطلبات، فمانخزّنش الـ host عليه."""
    from django.db import connection
    try:
        dom = connection.tenant.get_primary_domain()
        return f"https://{dom.domain}" if dom else ''
    except Exception:
        return ''


@admin.register(PrintCustomer)
class PrintCustomerAdmin(_CreatedByFormsetMixin, PrintSecureAdmin):
    list_display = ('name', 'company', 'phone', 'whatsapp', 'orders_count', 'balance_display',
                    'credit_display', 'statement_link')
    search_fields = ('name', 'company', 'phone')
    list_filter = ('created_at',)
    inlines = [OnAccountPaymentInline]
    actions = ['settle_open_orders_from_credit']
    readonly_fields = ('portal_box',)

    def portal_box(self, obj):
        """🔗 رابط بوابة العميل (طلباته وفواتيره وبروفاته وحسابه) — بيتبعت له واتساب."""
        if not obj or not obj.pk:
            return gettext("احفظ العميل الأول.")
        from django.urls import reverse
        from urllib.parse import quote
        btn = ('display:inline-block;margin:2px 4px 2px 0;padding:6px 12px;border-radius:8px;border:0;cursor:pointer;'
               'text-decoration:none;font-weight:700;font-size:0.82rem;color:#fff;background:{}')
        action = reverse('printing:customer_portal_link', args=[obj.pk])
        if not obj.portal_token:
            return format_html('<span id="portal"></span><button type="submit" formaction="{}" formmethod="post" style="' + btn + '">🔗 {}</button>',
                               action, '#8b5cf6', gettext('اعمل رابط بوابة للعميل'))
        url = _site_root() + reverse('printing:customer_portal', args=[obj.portal_token])
        parts = [format_html('<span id="portal"></span><input readonly value="{}" dir="ltr" onclick="this.select()" '
                             'style="width:100%;max-width:520px;padding:6px 10px;border:1px solid #cbd5e1;border-radius:8px;"><br>', url),
                 format_html('<a href="{}" target="_blank" style="' + btn + '">👁️ {}</a>', url, '#0f172a', gettext('افتح'))]
        from .production import _wa_number
        wa = _wa_number(obj.whatsapp or obj.phone)
        if wa:
            msg = gettext("أهلاً %(n)s 👋 ده رابط حسابك عندنا — طلباتك وفواتيرك والبروفات:") % {'n': obj.name}
            parts.append(format_html('<a href="https://wa.me/{}?text={}" target="_blank" rel="noopener" style="' + btn + '">💬 {}</a>',
                                     wa, quote(f"{msg}\n{url}"), '#25d366', gettext('ابعته واتساب')))
        parts.append(format_html('<button type="submit" formaction="{}" formmethod="post" name="regenerate" value="1" '
                                 'style="' + btn + '" onclick="return confirm(this.dataset.q);" data-q="{}">♻️ {}</button>',
                                 action, '#dc2626', gettext('الرابط القديم هيقف. متأكد؟'), gettext('رابط جديد')))
        return format_html(''.join(['{}'] * len(parts)), *parts)
    portal_box.short_description = _("🔗 بوابة العميل")

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
