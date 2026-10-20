"""
🎨 Mousstec Printing Admin — لوحة تحكم المطابع والتصميم
"""
from decimal import Decimal

from django.contrib import admin, messages
from django.urls import reverse
from django.utils.html import format_html
from django.utils.translation import gettext, gettext_lazy as _
from erp_core.localization import current_tenant_symbol as _cur_sym
from django.db.models import Sum, Count, Avg
from django.db import models, transaction
from django.utils import timezone
from django.db import connection
from django import forms
from django.core.exceptions import ValidationError

from . import ledger
from .models import (
    PrintBranch, PrintCustomer, MachineProfile, Designer,
    DesignerWorkLog, PrintOrder, PrintJob, PrintMaterial, PrintJobMaterial,
    PrintTreasury, PrintTransaction, ProductType, StaffPermission,
    PriceQuotation, QuotationLine,
    PrintSupplier, MaterialPurchase, MaterialPurchaseLine, PrintStockAdjustment,
    PrintProof, PrintCreditAllocation, PrintCreditNote, PrintFixedAsset, PrintExpenseBill,
    MaterialPurchaseReturn, MaterialPurchaseReturnLine, PrintTreasuryTransfer, PrintBankReconciliation,
    PrintVatReturn,
)


def _money(value):
    return format_html('<b>{}</b> {}', f"{float(value or 0):,.2f}", _cur_sym())


class _CreatedByFormsetMixin:
    """Stamp created_by on inline rows and show model ValidationErrors as messages."""

    def save_formset(self, request, form, formset, change):
        instances = formset.save(commit=False)
        for obj in formset.deleted_objects:
            try:
                obj.delete()
            except ValidationError as exc:
                messages.error(request, '؛ '.join(exc.messages))
        for inst in instances:
            if hasattr(inst, 'created_by_id') and not inst.created_by_id:
                inst.created_by = request.user
            try:
                inst.save()
            except ValidationError as exc:
                messages.error(request, '؛ '.join(exc.messages))
        formset.save_m2m()


class PrintTransactionForm(forms.ModelForm):
    """
    🛡️ Refuse an 'out' movement bigger than the treasury balance *as a form
    error*. The model still guards it, but there it's a raw exception that
    turned the admin page into a 500.
    """
    class Meta:
        model = PrintTransaction
        fields = '__all__'

    def clean(self):
        data = super().clean()
        treasury, kind, amount = data.get('treasury'), data.get('transaction_type'), data.get('amount')
        if not (treasury and kind and amount) or kind != 'out':
            return data
        available = treasury.balance
        inst = self.instance
        if inst.pk and inst.treasury_id == treasury.pk:
            # Editing: the row's current effect is already inside the balance.
            available += inst.amount if inst.transaction_type == 'out' else -inst.amount
        if amount > available:
            raise ValidationError(gettext("رصيد الخزنة «%(name)s» لا يكفي: المتاح %(available)s.")
                                  % {'name': treasury.name, 'available': f"{available:,.2f}"})
        return data


class PrintSecureAdmin(admin.ModelAdmin):
    """حماية: حظر الوصول من الـ public schema لجداول الطباعة"""
    def has_module_permission(self, request):
        if connection.schema_name == 'public':
            return False
        return super().has_module_permission(request)

    def has_view_permission(self, request, obj=None):
        if connection.schema_name == 'public':
            return False
        return super().has_view_permission(request, obj)

    # 🛡️ [FIX]: Block add/change/delete from public schema too
    def has_add_permission(self, request):
        if connection.schema_name == 'public':
            return False
        return super().has_add_permission(request)

    def has_change_permission(self, request, obj=None):
        if connection.schema_name == 'public':
            return False
        return super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        if connection.schema_name == 'public':
            return False
        return super().has_delete_permission(request, obj)


# =====================================================================
# 🏢 الفروع والعملاء
# =====================================================================

@admin.register(PrintBranch)
class PrintBranchAdmin(PrintSecureAdmin):
    list_display = ('name', 'phone', 'is_active')
    search_fields = ('name',)


class OnAccountPaymentInline(admin.TabularInline):
    """💳 دفعات على حساب العميل نفسه (من غير طلب) = رصيد مقدّم يتسدّد منه أي طلب بعدين."""
    model = PrintTransaction
    form = PrintTransactionForm
    fk_name = 'customer'
    extra = 0
    fields = ('treasury', 'transaction_type', 'amount', 'description', 'date')
    verbose_name = _("دفعة على الحساب")
    verbose_name_plural = _("💳 دفعات على حساب العميل (رصيد مقدّم — إيداع = دفعة، سحب = رد فلوس له)")

    def get_queryset(self, request):
        return super().get_queryset(request).filter(order__isnull=True)


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


# =====================================================================
# 🖨️ ماكينات الطباعة
# =====================================================================

@admin.register(MachineProfile)
class MachineProfileAdmin(PrintSecureAdmin):
    list_display = ('name', 'machine_type_badge', 'brand', 'branch', 'hourly_cost_display', 'status_badge')
    list_filter = ('machine_type', 'is_active', 'branch')
    search_fields = ('name', 'brand', 'model_number')
    list_select_related = ('branch',)
    fieldsets = (
        (_('📋 بيانات الماكينة'), {
            'fields': ('name', 'machine_type', 'brand', 'model_number', 'branch', 'is_active')
        }),
        (_('⚡ تكاليف التشغيل'), {
            'fields': ('power_consumption_kwh', 'electricity_rate_per_kwh', 'hourly_labor_cost'),
            'description': _('أدخل بيانات الاستهلاك لحساب تكلفة التشغيل تلقائياً')
        }),
        (_('🎨 تكلفة الأحبار (CMYK)'), {
            'fields': ('ink_cyan_cost_per_ml', 'ink_magenta_cost_per_ml', 'ink_yellow_cost_per_ml', 'ink_black_cost_per_ml'),
            'classes': ('collapse',)
        }),
        (_('📊 الصيانة والإحصائيات'), {
            'fields': ('total_print_hours', 'maintenance_due_date', 'notes'),
            'classes': ('collapse',)
        }),
    )

    def machine_type_badge(self, obj):
        colors = {
            'digital': '#3b82f6', 'offset': '#8b5cf6', 'large_format': '#f59e0b',
            'dtf': '#ec4899', 'uv': '#06b6d4', 'sublimation': '#ef4444',
            'cutter': '#10b981', 'laminator': '#6366f1', 'other': '#64748b',
        }
        c = colors.get(obj.machine_type, '#64748b')
        return format_html('<span style="background:{};color:white;padding:3px 10px;border-radius:12px;font-size:11px;font-weight:700;">{}</span>', c, obj.get_machine_type_display())
    machine_type_badge.short_description = _("النوع")

    def hourly_cost_display(self, obj):
        cost = obj.hourly_operating_cost
        return format_html('<b style="color:#f59e0b;">{} {}/{}</b>', f"{float(cost):,.2f}", _cur_sym(), gettext('ساعة'))
    hourly_cost_display.short_description = _("تكلفة التشغيل")

    def status_badge(self, obj):
        if obj.is_active:
            return format_html('<span style="color:#10b981;font-weight:bold;">🟢 {}</span>', gettext('تعمل'))
        return format_html('<span style="color:#ef4444;font-weight:bold;">🔴 {}</span>', gettext('متوقفة'))
    status_badge.short_description = _("الحالة")


# =====================================================================
# 🎨 المصممين وسجل الأعمال
# =====================================================================

class DesignerWorkLogInline(admin.TabularInline):
    model = DesignerWorkLog
    extra = 1
    fields = ('date', 'title', 'execution_type', 'duration_hours', 'client_rating', 'customer')


@admin.register(Designer)
class DesignerAdmin(PrintSecureAdmin):
    list_display = ('__str__', 'specialization', 'branch', 'month_works', 'month_hours', 'avg_rating', 'is_active')
    list_filter = ('is_active', 'branch')
    inlines = [DesignerWorkLogInline]

    def month_works(self, obj):
        stats = obj.get_month_stats()
        return stats.get('total_works') or 0
    month_works.short_description = _("أعمال الشهر")

    def month_hours(self, obj):
        stats = obj.get_month_stats()
        h = stats.get('total_hours') or 0
        return format_html('<b>{}</b> {}', f"{float(h):.1f}", gettext('ساعة'))
    month_hours.short_description = _("ساعات الشهر")

    def avg_rating(self, obj):
        stats = obj.get_month_stats()
        r = stats.get('avg_rating')
        if not r:
            return '-'
        stars = '⭐' * int(round(r))
        return format_html('<span title="{}">{}</span>', f"{r:.1f}", stars)
    avg_rating.short_description = _("التقييم")


@admin.register(DesignerWorkLog)
class DesignerWorkLogAdmin(PrintSecureAdmin):
    list_display = ('designer', 'title', 'execution_badge', 'duration_hours', 'rating_display', 'date')
    list_filter = ('execution_type', 'date', 'designer')
    search_fields = ('title', 'description')
    list_select_related = ('designer',)
    date_hierarchy = 'date'

    def execution_badge(self, obj):
        colors = {'manual': '#3b82f6', 'ai_generated': '#8b5cf6', 'ai_assisted': '#06b6d4'}
        c = colors.get(obj.execution_type, '#64748b')
        return format_html('<span style="background:{};color:white;padding:3px 8px;border-radius:8px;font-size:11px;font-weight:700;">{}</span>', c, obj.get_execution_type_display())
    execution_badge.short_description = _("نوع التنفيذ")

    def rating_display(self, obj):
        if obj.client_rating:
            return '⭐' * obj.client_rating
        return '-'
    rating_display.short_description = _("التقييم")


# =====================================================================
# 📋 طلبات ومهام الطباعة
# =====================================================================

class PrintOrderForm(forms.ModelForm):
    class Meta:
        model = PrintOrder
        fields = '__all__'

    def clean(self):
        data = super().clean()
        total = data.get('total_amount', self.instance.total_amount if self.instance.pk else None)
        discount = data.get('discount')
        # 🐛 [FIX]: الخصم كان ممكن يبقى أكبر من الإجمالي ⇒ إيراد بالسالب ومستحق بالسالب
        if total and discount and discount > total:
            self.add_error('discount', gettext("الخصم (%(discount)s) أكبر من إجمالي الطلب (%(total)s).")
                           % {'discount': f"{discount:,.2f}", 'total': f"{total:,.2f}"})
        return data


def _order_paid(order):
    """Net paid on an order straight from its transactions (paid_amount syncs on commit)."""
    agg = order.printtransaction_set.aggregate(
        ins=Sum('amount', filter=models.Q(transaction_type='in')),
        outs=Sum('amount', filter=models.Q(transaction_type='out')))
    return (agg['ins'] or Decimal('0')) - (agg['outs'] or Decimal('0'))


def _wa_number(raw):
    """Egyptian mobile → wa.me international digits (01x… → 201x…)."""
    digits = ''.join(ch for ch in str(raw or '') if ch.isdigit())
    if digits.startswith('00'):
        digits = digits[2:]
    if digits.startswith('01') and len(digits) == 11:
        digits = '2' + digits
    return digits if len(digits) >= 10 else ''


class PrintJobInlineForm(forms.ModelForm):
    """سطر مهمة سريع من غير ما تختار مرحلة = «تصميم» (أو المرحلة اللي هو عليها)."""

    class Meta:
        model = PrintJob
        fields = '__all__'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if 'stage' in self.fields:
            self.fields['stage'].required = False

    def clean_stage(self):
        return self.cleaned_data.get('stage') or self.instance.stage or 'design'


class PrintJobInline(admin.TabularInline):
    model = PrintJob
    form = PrintJobInlineForm
    extra = 1
    fields = ('product_type_text', 'description', 'machine', 'paper_size', 'quantity', 'copies', 'unit_price', 'total_price', 'design_file', 'stage', 'proof_waived', 'is_complete')
    # 🐛 [FIX]: الإجمالي كان خانة بتتكتب — وبعدين بيتحسب من جديد (سعر × كمية × نسخ)
    # فاللي اتكتب بيضيع من غير ما حد يعرف. بقى للعرض بس.
    readonly_fields = ('total_price',)
    autocomplete_fields = ['product_type']


class PrintOrderPaymentInline(admin.TabularInline):
    """💵 Record payments / refunds straight from the order page."""
    model = PrintTransaction
    form = PrintTransactionForm
    fk_name = 'order'
    extra = 1
    fields = ('treasury', 'transaction_type', 'amount', 'description', 'date')
    verbose_name = _("دفعة / استرداد")
    verbose_name_plural = _("💵 الدفعات على الطلب (إيداع = دفعة من العميل، سحب = استرداد له)")


class PrintCreditNoteInline(admin.TabularInline):
    """↩️ المرتجعات/المسموحات بعد التسليم (للعرض — الإضافة من زرار «إشعار دائن»)."""
    model = PrintCreditNote
    extra = 0
    max_num = 0
    can_delete = False
    fields = ('number', 'date', 'reason', 'amount', 'tax_amount', 'refund_treasury')
    readonly_fields = fields
    verbose_name = _("إشعار دائن")
    verbose_name_plural = _("↩️ المرتجعات والمسموحات بعد التسليم (إشعارات دائنة)")


class PrintCreditAllocationInline(admin.TabularInline):
    """💳 تسديد من الرصيد المقدّم على حساب العميل — من غير حركة خزنة."""
    model = PrintCreditAllocation
    extra = 0
    fields = ('amount', 'date', 'notes')
    verbose_name = _("تسديد من الرصيد المقدّم")
    verbose_name_plural = _("💳 تسديد من الرصيد المقدّم على حساب العميل (من غير ما الفلوس تتحرك في الخزنة)")


@admin.register(PrintOrder)
class PrintOrderAdmin(PrintSecureAdmin):
    form = PrintOrderForm
    list_display = ('order_number', 'customer', 'status_badge', 'production_badge', 'total_display', 'paid_display', 'remaining_display', 'profit_badge', 'has_files_badge', 'due_display', 'date_created')
    list_filter = ('status', 'branch', 'date_created')
    search_fields = ('order_number', 'customer__name', 'customer__phone')
    list_select_related = ('customer', 'branch')
    date_hierarchy = 'date_created'
    inlines = [PrintJobInline, PrintOrderPaymentInline, PrintCreditAllocationInline, PrintCreditNoteInline]
    # 🐛 [FIX]: رقم الطلب كان مطلوب يتكتب بإيدك في «طلب جديد» — فالترقيم
    # التلقائي PO-YYMMDD-NNN عمره ما اشتغل من الأدمن. دلوقتي بيتولّد لوحده.
    readonly_fields = ('order_number', 'order_actions', 'paid_amount', 'date_delivered')
    actions = ['cancel_and_refund', 'apply_customer_credit']

    def get_inlines(self, request, obj):
        # التسديد من الرصيد المقدّم بيتحسب على طلب محفوظ (المتبقّي عليه) — مش في «طلب جديد».
        inlines = list(super().get_inlines(request, obj))
        if obj is None or not obj.pk:
            inlines.remove(PrintCreditAllocationInline)
            inlines.remove(PrintCreditNoteInline)
        return inlines

    def get_list_display(self, request):
        cols = list(super().get_list_display(request))
        # 🔐 عمود الربح للي معاه «مشاهدة الأرباح» بس
        from printing.views.finance import _can_see_money
        if not _can_see_money(request.user, 'can_view_profits'):
            cols.remove('profit_badge')
        return cols

    def get_deleted_objects(self, objs, request):
        """🐛 [FIX]: حذف طلب عليه دفعات كان بيسيب الفلوس في الخزنة من غير طلب —
        فتختفي من كشف حساب العميل. الطلب ده يتلغي (ويترد المدفوع) مش يتحذف."""
        deleted, counts, perms_needed, protected = super().get_deleted_objects(objs, request)
        for order in objs:
            n = order.printtransaction_set.count()
            if n:
                protected = list(protected) + [gettext(
                    'طلب #%(no)s عليه %(n)s حركة دفع — استخدم «إلغاء الطلب ورد المدفوع» بدل الحذف')
                    % {'no': order.order_number, 'n': n}]
        return deleted, counts, perms_needed, protected

    @admin.action(description=_("❌ إلغاء الطلبات ورد المدفوع للعميل (سحب من نفس الخزنة)"))
    def cancel_and_refund(self, request, queryset):
        from printing.views.finance import _can_see_money
        if not _can_see_money(request.user, 'can_manage_treasury'):
            self.message_user(request, gettext("محتاج صلاحية «إدارة الخزينة» عشان ترد فلوس."), messages.ERROR)
            return
        for order in queryset:
            if order.status == 'delivered':
                self.message_user(request, gettext("#%(no)s اتسلّم بالفعل — لو العميل رجّع حاجة اعمل «إشعار دائن» من صفحة الطلب.")
                                  % {'no': order.order_number}, messages.WARNING)
                continue
            if order.status == 'cancelled' and _order_paid(order) <= 0:
                continue
            try:
                refunded = cancel_order_and_refund(order, user=request.user)
            except ValueError as exc:
                self.message_user(request, f"#{order.order_number}: {exc}", messages.ERROR)
                continue
            msg = gettext("#%(no)s اتلغى") % {'no': order.order_number}
            if refunded:
                msg += ' — ' + gettext("اترد للعميل %(amount)s") % {'amount': f"{refunded:,.2f} {_cur_sym()}"}
            self.message_user(request, msg, messages.SUCCESS)

    def order_actions(self, obj):
        if not obj or not obj.pk:
            return '—'
        btn = ('display:inline-block;margin:2px 4px 2px 0;padding:6px 12px;border-radius:8px;'
               'text-decoration:none;font-weight:700;font-size:0.82rem;color:#fff;background:{}')
        parts = [
            format_html('<a href="{}" target="_blank" style="' + btn + '">🧾 {}</a>',
                        reverse('printing:order_invoice', args=[obj.pk]), '#0f172a', gettext('الفاتورة / الإيصال')),
            format_html('<a href="{}?ticket=1" target="_blank" style="' + btn + '">🛠️ {}</a>',
                        reverse('printing:order_invoice', args=[obj.pk]), '#f97316', gettext('أمر الشغل للإنتاج')),
            format_html('<a href="{}" target="_blank" style="' + btn + '">📒 {}</a>',
                        reverse('printing:customer_statement', args=[obj.customer_id]), '#8b5cf6', gettext('كشف حساب العميل')),
        ]
        if obj.status == 'delivered':
            parts.append(format_html('<a href="{}?order={}" style="' + btn + '">↩️ {}</a>',
                                     reverse('admin:printing_printcreditnote_add'), obj.pk, '#dc2626',
                                     gettext('إشعار دائن (مرتجع / خصم بعد التسليم)')))
        wa = _wa_number(obj.customer.whatsapp or obj.customer.phone)
        if wa:
            from urllib.parse import quote
            parts.append(format_html('<a href="https://wa.me/{}?text={}" target="_blank" rel="noopener" style="' + btn + '">💬 {}</a>',
                                     wa, quote(order_whatsapp_text(obj)), '#25d366', gettext('إبلاغ العميل واتساب')))
        return format_html(''.join(['{}'] * len(parts)), *parts)
    order_actions.short_description = _("إجراءات سريعة")

    def due_display(self, obj):
        if not obj.date_due:
            return '—'
        late = obj.status in ('draft', 'confirmed', 'in_progress', 'ready') and obj.date_due < timezone.now()
        return format_html('<span style="color:{};font-weight:{};">{}{}</span>',
                           '#ef4444' if late else 'inherit', 700 if late else 400,
                           '⏰ ' if late else '', timezone.localtime(obj.date_due).strftime('%d/%m %H:%M'))
    due_display.short_description = _("موعد التسليم")
    due_display.admin_order_field = 'date_due'

    @admin.action(description=_("💳 سدّد من رصيد العميل المقدّم"))
    def apply_customer_credit(self, request, queryset):
        for order in queryset.select_related('customer'):
            try:
                amount = PrintCreditAllocation.apply_available(order, user=request.user)
            except ValidationError as exc:
                messages.error(request, f"#{order.order_number}: {'؛ '.join(exc.messages)}")
                continue
            if amount:
                messages.success(request, gettext("#%(no)s: اتسدّد %(amount)s من الرصيد المقدّم.")
                                 % {'no': order.order_number, 'amount': f"{amount:,.2f}"})
            else:
                messages.info(request, gettext("#%(no)s: مفيش رصيد مقدّم متاح أو الطلب متسدّد.")
                              % {'no': order.order_number})

    def production_badge(self, obj):
        stages = list(obj.jobs.values_list('stage', flat=True))
        if not stages:
            return '-'
        done = sum(1 for st in stages if st == 'done')
        waiting = sum(1 for st in stages if st == 'proof')
        extra = ' · ⏳ ' + (gettext('%(n)s بروفة') % {'n': waiting}) if waiting else ''
        return format_html('<span style="font-weight:700;">{}/{} {}{}</span>', done, len(stages), gettext('خلصت'), extra)
    production_badge.short_description = _("الإنتاج")

    def save_model(self, request, obj, form, change):
        obj._previous_status = (PrintOrder.objects.filter(pk=obj.pk).values_list('status', flat=True).first()
                                if change else None)
        super().save_model(request, obj, form, change)

    def get_readonly_fields(self, request, obj=None):
        ro = list(super().get_readonly_fields(request, obj))
        # Priced jobs drive the total (see PrintOrder.recalc_total_from_jobs).
        if obj and obj.pk and obj.jobs.filter(total_price__gt=0).exists():
            ro.append('total_amount')
        return ro

    def save_formset(self, request, form, formset, change):
        instances = formset.save(commit=False)
        for obj in formset.deleted_objects:
            obj.delete()
        for inst in instances:
            if hasattr(inst, 'created_by_id') and not inst.created_by_id:
                inst.created_by = request.user
            try:
                inst.save()
            except ValidationError as exc:
                messages.error(request, '؛ '.join(exc.messages))
        formset.save_m2m()

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        order = form.instance
        order.recalc_total_from_jobs()
        moved = order.sync_status_from_jobs()
        if moved:
            self.message_user(request, gettext("حالة الطلب بقت «%(status)s» حسب المهام.")
                              % {'status': order.get_status_display()}, messages.INFO)
        # 💳 حد الائتمان: الطلب اللي اتأكد والعميل عدّى الحد بيرجع مسودة.
        hold = order.apply_credit_hold(previous_status=getattr(order, '_previous_status', None))
        if hold:
            messages.warning(request, hold)
        ledger.on_order_changed(order.pk)
        self._lifecycle_warnings(request, order)

    def _lifecycle_warnings(self, request, order):
        """Tell the user what a status change means for money/production."""
        paid = _order_paid(order)
        cur = _cur_sym()
        if order.status == 'cancelled' and paid > 0:
            self.message_user(request, gettext(
                "الطلب اتلغى والعميل دافع عليه %(amount)s — لسه محسوبين له. "
                "لو هترجعهم: اختار الطلب من القايمة ← «إلغاء الطلبات ورد المدفوع». "
                "لو هيفضلوا رصيد له هيظهروا في كشف حسابه.") % {'amount': f"{paid:,.2f} {cur}"}, messages.WARNING)
            return
        if order.status in ('ready', 'delivered') and order.jobs.filter(is_complete=False).exists():
            self.message_user(request, gettext("فيه مهام لسه مش مكتملة في الطلب ده — علّمها «مكتملة» عشان تكلفتها تتثبت."), messages.WARNING)
        remaining = order.due_amount - paid
        if order.status == 'delivered' and remaining > 0:
            self.message_user(request, gettext("اتسلّم والعميل لسه عليه %(amount)s — هيفضل في «مستحقات العملاء» وكشف حسابه.")
                              % {'amount': f"{remaining:,.2f} {cur}"}, messages.WARNING)
        elif remaining < 0:
            self.message_user(request, gettext("العميل دافع زيادة %(amount)s عن قيمة الطلب — رصيد له.")
                              % {'amount': f"{-remaining:,.2f} {cur}"}, messages.WARNING)
        if order.discount and order.total_amount and order.discount > order.total_amount:
            self.message_user(request, gettext("الخصم أكبر من إجمالي المهام — راجع الخصم."), messages.ERROR)

    fieldsets = (
        (_('📋 بيانات الطلب'), {
            'fields': ('order_number', 'customer', 'branch', 'status', 'date_due', 'date_delivered', 'order_actions'),
        }),
        (_('💰 المالي'), {
            'fields': ('total_amount', 'discount', 'tax_amount', 'paid_amount'),
            'description': _('الإجمالي بيتحسب تلقائياً من أسعار المهام (لو فيه مهام مسعّرة). '
                             'المدفوع بيتحسب من الدفعات تحت — سجّل الدفعة في جدول «الدفعات على الطلب».'),
        }),
        (_('📁 ملفات المشروع'), {
            'fields': ('project_file', 'project_file_2', 'project_file_3'),
            'description': _('ارفع ملفات المشروع الأصلية (PSD, AI, PDF, إلخ) — يتم حفظها بأمان على السيرفر'),
        }),
        (_('📝 ملاحظات'), {
            'fields': ('notes',),
            'classes': ('collapse',),
        }),
    )

    def has_files_badge(self, obj):
        count = sum(1 for f in [obj.project_file, obj.project_file_2, obj.project_file_3] if f)
        if count:
            return format_html('<span style="background:#8b5cf6;color:white;padding:2px 8px;border-radius:8px;font-size:11px;font-weight:bold;">📁 {}</span>', count)
        return '-'
    has_files_badge.short_description = _("ملفات")

    def status_badge(self, obj):
        colors = {
            'draft': '#94a3b8', 'confirmed': '#3b82f6', 'in_progress': '#f59e0b',
            'ready': '#8b5cf6', 'delivered': '#10b981', 'cancelled': '#ef4444',
        }
        c = colors.get(obj.status, '#64748b')
        return format_html('<span style="background:{};color:white;padding:3px 10px;border-radius:12px;font-size:11px;font-weight:700;">{}</span>', c, obj.get_status_display())
    status_badge.short_description = _("الحالة")

    def total_display(self, obj):
        return format_html('<b>{}</b> {}', f"{float(obj.net_total):,.2f}", _cur_sym())
    total_display.short_description = _("الإجمالي")

    def paid_display(self, obj):
        return format_html('<span style="color:#10b981;font-weight:bold;">{}</span> {}', f"{float(obj.paid_amount):,.2f}", _cur_sym())
    paid_display.short_description = _("المدفوع")

    def remaining_display(self, obj):
        r = obj.remaining
        color = '#ef4444' if r > 0 else '#10b981'
        return format_html('<span style="color:{};font-weight:bold;">{}</span> {}', color, f"{float(r):,.2f}", _cur_sym())
    remaining_display.short_description = _("المتبقي")

    def profit_badge(self, obj):
        profit = obj.gross_profit
        margin = obj.profit_margin_percent
        if profit > 0:
            bg = 'linear-gradient(135deg,#10b981,#059669)'
            icon = '📈'
        elif profit < 0:
            bg = 'linear-gradient(135deg,#ef4444,#dc2626)'
            icon = '📉'
        else:
            bg = 'linear-gradient(135deg,#94a3b8,#64748b)'
            icon = '⚖️'
        return format_html(
            '<a href="/printing/order/{}/profit/" target="_blank" '
            'style="background:{}; color:#fff; padding:4px 10px; border-radius:10px; '
            'text-decoration:none; font-weight:700; font-size:0.78rem; display:inline-block;" '
            'title="{}">{} {} {} ({}%)</a>',
            obj.pk, bg, gettext('افتح تحليل الربحية'), icon, f"{float(profit):,.0f}", _cur_sym(), f"{float(margin):.1f}",
        )
    profit_badge.short_description = _("الربح")


def cancel_order_and_refund(order, *, user=None):
    """Cancel ``order`` and pay back what the customer has net paid on it.

    The refund leaves from the same treasury(ies) the payments went into.
    Raises ValueError (nothing changed) when a treasury can't cover it.
    Returns the refunded amount.
    """
    refunded = Decimal('0')
    with transaction.atomic():
        order = PrintOrder.objects.select_for_update().get(pk=order.pk)
        per_treasury = (PrintTransaction.objects.filter(order=order)
                        .values('treasury_id')
                        .annotate(ins=Sum('amount', filter=models.Q(transaction_type='in')),
                                  outs=Sum('amount', filter=models.Q(transaction_type='out'))))
        for row in per_treasury:
            net = (row['ins'] or Decimal('0')) - (row['outs'] or Decimal('0'))
            if net <= 0:
                continue
            PrintTransaction.objects.create(
                treasury_id=row['treasury_id'], transaction_type='out', amount=net, order=order,
                description=f'استرداد — إلغاء الطلب #{order.order_number}', created_by=user)
            refunded += net
        # 💳 اللي اتسدّد من الرصيد المقدّم مش فلوس اتدفعت على الطلب ده — بيرجع رصيد للعميل.
        order.credit_allocations.all().delete()
        order.status = 'cancelled'
        order.save(update_fields=['status'])
    return refunded


def order_whatsapp_text(order):
    """Customer-facing WhatsApp message for the order's current state."""
    cur = _cur_sym()
    name = order.customer.name
    status_line = {
        'draft': 'استلمنا طلبك وبنجهّزه',
        'confirmed': 'طلبك اتأكد وهيدخل الإنتاج',
        'in_progress': 'طلبك في الإنتاج دلوقتي',
        'ready': 'طلبك جاهز للاستلام ✅',
        'delivered': 'طلبك اتسلّم — شكراً لثقتك 🙏',
        'cancelled': 'طلبك اتلغى',
    }.get(order.status, '')
    lines = [f'أهلاً {name}،', f'طلب رقم {order.order_number}: {status_line}.',
             f'الإجمالي: {order.net_total:,.2f} {cur}']
    remaining = order.remaining
    if order.status != 'cancelled':
        lines.append(f'المدفوع: {order.paid_amount:,.2f} {cur} — المتبقي: {remaining:,.2f} {cur}' if remaining > 0
                     else 'الحساب خالص ✅')
    shop = getattr(connection, 'tenant', None)
    if shop is not None and getattr(shop, 'name', ''):
        lines.append(shop.name)
    return '\n'.join(lines)


class PrintJobMaterialForm(forms.ModelForm):
    class Meta:
        model = PrintJobMaterial
        fields = ('material', 'quantity')

    def clean(self):
        data = super().clean()
        material, qty = data.get('material'), data.get('quantity')
        if material and qty:
            available = material.quantity
            inst = self.instance
            if inst.pk and inst.material_id == material.pk:
                available += inst.quantity   # this line's current draw is already out of stock
            if qty > available:
                raise ValidationError(
                    gettext("المخزون من «%(name)s» مش كفاية: المتاح %(available)s %(unit)s.")
                    % {'name': material.name, 'available': available, 'unit': material.unit})
        return data


class PrintJobMaterialInline(admin.TabularInline):
    """🧾 Materials used on this job — deducted from stock, added to its cost."""
    model = PrintJobMaterial
    form = PrintJobMaterialForm
    extra = 1
    fields = ('material', 'quantity', 'unit_cost')
    readonly_fields = ('unit_cost',)
    verbose_name = _("خامة مصروفة")
    verbose_name_plural = _("🧾 الخامات المصروفة (بتتخصم من المخزون وتدخل في التكلفة)")


class PrintProofInline(admin.TabularInline):
    """🖼️ بروفات المهمة — ارفع البروفة وابعت الرابط للعميل يوافق أو يطلب تعديل."""
    model = PrintProof
    extra = 0
    fields = ('version', 'file', 'notes', 'status', 'customer_comment', 'share_links')
    readonly_fields = ('version', 'status', 'customer_comment', 'share_links')
    verbose_name = _("بروفة")
    verbose_name_plural = _("🖼️ البروفات (العميل بيوافق من الرابط — الطباعة مستنية موافقته)")

    def share_links(self, obj):
        if not obj or not obj.pk:
            return '-'
        return _proof_links(obj)
    share_links.short_description = _("رابط العميل")


def _proof_links(proof):
    customer = proof.job.order.customer
    number = ''.join(ch for ch in (customer.whatsapp or customer.phone or '') if ch.isdigit())
    if number.startswith('0'):
        number = '2' + number
    from urllib.parse import quote as _urlquote
    msg = _urlquote(f"بروفة «{proof.job.description[:60]}» — راجعها ووافق أو اطلب تعديل: "
                    f"/printing/proof/{proof.share_token}/")
    return format_html(
        '<a href="/printing/proof/{}/" target="_blank" style="font-weight:700;">🔗 {}</a> · '
        '<a href="https://wa.me/{}?text={}" target="_blank" style="color:#16a34a;font-weight:700;">{}</a>',
        proof.share_token, gettext('فتح'), number, msg, gettext('واتساب'))


STAGE_COLORS = {'design': '#94a3b8', 'proof': '#f59e0b', 'printing': '#3b82f6',
                'finishing': '#8b5cf6', 'done': '#10b981'}


@admin.register(PrintJob)
class PrintJobAdmin(_CreatedByFormsetMixin, PrintSecureAdmin):
    list_display = ('description', 'product_type_badge', 'order', 'stage_badge', 'machine', 'quantity', 'total_price', 'cost_display', 'profit_display', 'is_complete')
    list_filter = ('stage', 'is_complete', 'machine', 'product_type', 'paper_size')
    search_fields = ('description', 'product_type_text', 'order__order_number')
    list_select_related = ('order', 'machine', 'product_type')
    autocomplete_fields = ['product_type']
    inlines = [PrintJobMaterialInline, PrintProofInline]
    actions = ['advance_stage']
    readonly_fields = ('total_price', 'completed_at')

    def stage_badge(self, obj):
        return format_html('<span style="background:{};color:white;padding:3px 10px;border-radius:12px;'
                           'font-size:11px;font-weight:700;">{}</span>',
                           STAGE_COLORS.get(obj.stage, '#64748b'), obj.get_stage_display())
    stage_badge.short_description = _("المرحلة")

    @admin.action(description=_("➡️ انقل للمرحلة اللي بعدها (تصميم ← بروفة ← طباعة ← تشطيب ← خلصت)"))
    def advance_stage(self, request, queryset):
        for job in queryset:
            try:
                stage = job.advance_stage()
                messages.success(request, f"«{job.description[:40]}» ← {dict(PrintJob.STAGE_CHOICES)[stage]}")
            except ValidationError as exc:
                messages.warning(request, '؛ '.join(exc.messages))

    def product_type_badge(self, obj):
        name = obj.product_type_text or (obj.product_type.name if obj.product_type else '-')
        if name == '-':
            return '-'
        return format_html('<span style="background:#6366f1;color:white;padding:2px 8px;border-radius:8px;font-size:11px;font-weight:bold;">{}</span>', name)
    product_type_badge.short_description = _("نوع البند")

    def cost_display(self, obj):
        cost = obj.calculated_cost
        return format_html('<span style="color:#f59e0b;">{}</span> {}', f"{float(cost):,.2f}", _cur_sym())
    cost_display.short_description = _("التكلفة الفعلية")

    def profit_display(self, obj):
        p = obj.profit
        color = '#10b981' if p >= 0 else '#ef4444'
        return format_html('<span style="color:{};font-weight:bold;">{}</span> {}', color, f"{float(p):,.2f}", _cur_sym())
    profit_display.short_description = _("الربح")


# =====================================================================
# 📦 خامات الطباعة
# =====================================================================

@admin.register(PrintMaterial)
class PrintMaterialAdmin(PrintSecureAdmin):
    list_display = ('name', 'category', 'quantity_display', 'cost_display', 'stock_value_display', 'stock_alert')
    list_filter = ('category', 'branch')
    search_fields = ('name', 'sku')
    list_select_related = ('branch',)

    def get_readonly_fields(self, request, obj=None):
        # 🐛 [FIX]: الكمية والتكلفة كانوا بيتكتبوا بالإيد فالمخزون بيتغيّر من غير
        #    مستند ولا قيد. بعد الإضافة (= المخزون الافتتاحي) بيتحركوا من فواتير
        #    الشراء والصرف على الشغل وتسويات المخزن بس.
        ro = list(super().get_readonly_fields(request, obj))
        if obj and obj.pk:
            ro += ['quantity', 'cost_per_unit', 'stock_moves']
        return ro

    def get_fields(self, request, obj=None):
        fields = list(super().get_fields(request, obj))
        if obj and obj.pk:
            fields.append('stock_moves')
        return fields

    def stock_moves(self, obj):
        return format_html(
            '<a href="/secure-portal/printing/materialpurchase/add/">📥 {}</a> · '
            '<a href="/secure-portal/printing/printstockadjustment/add/?material={}">⚖️ {}</a>',
            gettext('فاتورة شراء'), obj.pk, gettext('تسوية جرد / تالف'))
    stock_moves.short_description = _("تحريك المخزون")

    def cost_display(self, obj):
        return f"{float(obj.cost_per_unit):,.4f}".rstrip('0').rstrip('.')
    cost_display.short_description = _("تكلفة الوحدة")

    def quantity_display(self, obj):
        return format_html('<b>{}</b> {}', f"{float(obj.quantity):,.1f}", obj.unit)
    quantity_display.short_description = _("الكمية")

    def stock_value_display(self, obj):
        return format_html('<b>{}</b> {}', f"{float(obj.stock_value):,.2f}", _cur_sym())
    stock_value_display.short_description = _("القيمة")

    def stock_alert(self, obj):
        if obj.is_low_stock:
            return format_html('<span style="background:#ef4444;color:white;padding:2px 8px;border-radius:8px;font-size:11px;font-weight:bold;">⚠️ {}</span>', gettext('منخفض'))
        return format_html('<span style="color:#10b981;">✅ {}</span>', gettext('متوفر'))
    stock_alert.short_description = _("المخزون")


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
        if obj.kind == 'cash':
            return '—'
        return format_html('<a href="/printing/accounting/reconcile/{}/">🏦 {}</a>', obj.pk, gettext('مطابقة مع الكشف'))
    reconcile_link.short_description = _("مطابقة البنك")

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


@admin.register(PrintTransaction)
class PrintTransactionAdmin(PrintSecureAdmin):
    form = PrintTransactionForm
    list_display = ('type_badge', 'amount_display', 'category', 'treasury', 'party', 'description', 'date')
    list_filter = ('transaction_type', 'category', 'treasury', 'date')
    search_fields = ('description', 'order__order_number', 'customer__name', 'supplier__name')
    list_select_related = ('treasury', 'order', 'customer', 'supplier', 'purchase')
    exclude = ('created_by',)
    readonly_fields = ('fixed_asset',)

    def delete_model(self, request, obj):
        try:
            super().delete_model(request, obj)
        except ValidationError as exc:      # رجل تحويل بين خزنتين
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


# =====================================================================
# 🏷️ أنواع البنود
# =====================================================================

@admin.register(ProductType)
class ProductTypeAdmin(PrintSecureAdmin):
    list_display = ('name', 'usage_count_display', 'created_at')
    search_fields = ('name',)
    ordering = ('-usage_count',)

    def usage_count_display(self, obj):
        return format_html('<b style="color:#6366f1;">{}</b> {}', obj.usage_count, gettext('مرة'))
    usage_count_display.short_description = _("عدد الاستخدام")


# =====================================================================
# 🔐 صلاحيات الموظفين
# =====================================================================

@admin.register(StaffPermission)
class StaffPermissionAdmin(PrintSecureAdmin):
    list_display = (
        'user', 'can_view_treasury', 'can_view_profits',
        'can_view_project_files', 'can_use_ai_studio',
        'can_manage_stock', 'can_view_reports',
    )
    list_filter = (
        'can_view_treasury', 'can_view_profits',
        'can_use_ai_studio', 'can_view_reports',
    )
    list_editable = (
        'can_view_treasury', 'can_view_profits',
        'can_view_project_files', 'can_use_ai_studio',
        'can_manage_stock', 'can_view_reports',
    )
    fieldsets = (
        (_('👤 الموظف'), {'fields': ('user',)}),
        (_('💰 الصلاحيات المالية'), {
            'fields': ('can_view_treasury', 'can_manage_treasury', 'can_view_profits'),
            'description': _('⚠️ صلاحيات حساسة — فقط للمديرين والمحاسبين'),
        }),
        (_('📋 الطلبات'), {
            'fields': ('can_create_orders', 'can_edit_orders', 'can_delete_orders', 'can_view_all_orders'),
        }),
        (_('📁 الملفات والعملاء'), {
            'fields': ('can_view_project_files', 'can_upload_project_files', 'can_manage_customers'),
        }),
        (_('📦 المخزون والمصممين'), {
            'fields': ('can_manage_stock', 'can_view_designers'),
        }),
        (_('🤖 AI وتقارير'), {
            'fields': ('can_use_ai_studio', 'can_view_reports'),
        }),
    )


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
# 🚚 الموردين ومشتريات الخامات
# =====================================================================

class SupplierPaymentInline(admin.TabularInline):
    """💸 دفعات على حساب المورد (من غير فاتورة) — دفعة مقدمة أو سداد رصيد قديم."""
    model = PrintTransaction
    form = PrintTransactionForm
    fk_name = 'supplier'
    extra = 0
    fields = ('treasury', 'transaction_type', 'amount', 'description', 'date')
    verbose_name = _("دفعة على حساب المورد")
    verbose_name_plural = _("💸 دفعات على حساب المورد (سحب = دفعنا له، إيداع = رجّعلنا فلوس)")

    def get_queryset(self, request):
        return super().get_queryset(request).filter(purchase__isnull=True, bill__isnull=True)


@admin.register(PrintSupplier)
class PrintSupplierAdmin(_CreatedByFormsetMixin, PrintSecureAdmin):
    list_display = ('name', 'company', 'phone', 'balance_display', 'is_active', 'statement_link')
    list_filter = ('is_active',)
    search_fields = ('name', 'company', 'phone')
    inlines = [SupplierPaymentInline]

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
    fields = ('treasury', 'transaction_type', 'amount', 'description', 'date')
    verbose_name = _("دفعة")
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
    inlines = [MaterialPurchaseLineInline, PurchasePaymentInline]
    actions = ['receive_selected', 'cancel_selected']
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
        return format_html(
            '{} {} − {} {} + {} {} = <b>{}</b> · {} {} · {} <b>{}</b> · {} <b>{}</b>',
            gettext('البنود'), f"{obj.total_amount:,.2f}", gettext('خصم'), f"{obj.discount:,.2f}",
            gettext('ضريبة'), f"{obj.tax_amount:,.2f}", f"{obj.net_total:,.2f}",
            gettext('المدفوع'), f"{obj.paid_amount:,.2f}", gettext('الباقي'), f"{obj.remaining:,.2f}",
            gettext('الحالة:'), obj.get_status_display())
    totals.short_description = _("الإجمالي")

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


# =====================================================================
# ⚖️ تسويات المخزن
# =====================================================================

class PrintStockAdjustmentForm(forms.ModelForm):
    counted_quantity = forms.DecimalField(
        required=False, max_digits=12, decimal_places=2, label=_("الكمية الفعلية بعد الجرد"),
        help_text=_("لو عملت جرد اكتب الموجود فعلاً — السيستم يحسب الفرق لوحده."))

    class Meta:
        model = PrintStockAdjustment
        fields = ('material', 'counted_quantity', 'quantity_change', 'reason', 'notes')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['quantity_change'].required = False

    def clean(self):
        data = super().clean()
        material, counted = data.get('material'), data.get('counted_quantity')
        if material is not None and counted is not None:
            data['quantity_change'] = counted - material.quantity
            self.instance.quantity_change = data['quantity_change']
        change = data.get('quantity_change')
        if not change:
            raise ValidationError(_("اكتب الكمية الفعلية بعد الجرد أو التغيير في الكمية (ومينفعش يبقى صفر)."))
        if material is not None and material.quantity + change < 0:
            raise ValidationError(_("المتاح من «%(m)s» %(q)s %(u)s بس.")
                                  % {'m': material.name, 'q': material.quantity, 'u': material.unit})
        return data


@admin.register(PrintStockAdjustment)
class PrintStockAdjustmentAdmin(PrintSecureAdmin):
    form = PrintStockAdjustmentForm
    list_display = ('material', 'change_display', 'value_display', 'reason', 'notes', 'created_by', 'created_at')
    list_filter = ('reason', 'material', 'created_at')
    search_fields = ('material__name', 'notes')
    list_select_related = ('material', 'created_by')

    def has_change_permission(self, request, obj=None):
        # التسوية مابتتعدّلش — تتمسح (الكمية ترجع) وتتعمل من جديد.
        return obj is None and super().has_change_permission(request, obj)

    def save_model(self, request, obj, form, change):
        if not obj.created_by_id:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)

    def delete_model(self, request, obj):
        try:
            super().delete_model(request, obj)
        except ValidationError as exc:
            messages.error(request, '؛ '.join(exc.messages))

    def delete_queryset(self, request, queryset):
        for obj in queryset:
            self.delete_model(request, obj)

    def change_display(self, obj):
        color = '#10b981' if obj.quantity_change > 0 else '#ef4444'
        sign = '+' if obj.quantity_change > 0 else ''
        return format_html('<b style="color:{};">{}{}</b> {}', color, sign, obj.quantity_change, obj.material.unit)
    change_display.short_description = _("التغيير")

    def value_display(self, obj):
        return _money(obj.value)
    value_display.short_description = _("القيمة")


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

class _SafeSaveDeleteMixin:
    """أخطاء الموديل (فترة مقفولة، رصيد مش كفاية…) تظهر رسالة بدل صفحة 500."""

    def save_model(self, request, obj, form, change):
        if hasattr(obj, 'created_by_id') and not obj.created_by_id:
            obj.created_by = request.user
        try:
            super().save_model(request, obj, form, change)
        except ValidationError as exc:
            messages.error(request, '؛ '.join(exc.messages))

    def delete_model(self, request, obj):
        try:
            obj.delete()
        except ValidationError as exc:
            messages.error(request, '؛ '.join(exc.messages))

    def delete_queryset(self, request, queryset):
        for obj in queryset:
            self.delete_model(request, obj)


def _amount_html(value, color='inherit'):
    return format_html('<b style="color:{};">{} {}</b>', color, f"{float(value or 0):,.2f}", _cur_sym())


class PrintCreditNoteForm(forms.ModelForm):
    class Meta:
        model = PrintCreditNote
        fields = ('order', 'date', 'reason', 'amount', 'tax_amount', 'refund_treasury')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if 'order' in self.fields:
            self.fields['order'].queryset = PrintOrder.objects.filter(status='delivered').select_related('customer')

    def clean(self):
        data = super().clean()
        order, amount, treasury = data.get('order'), data.get('amount'), data.get('refund_treasury')
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
    list_display = ('number', 'order', 'customer', 'date', 'total_display', 'refund_treasury', 'reason')
    list_select_related = ('order__customer', 'refund_treasury')
    search_fields = ('number', 'order__order_number', 'order__customer__name', 'reason')
    date_hierarchy = 'date'

    def has_change_permission(self, request, obj=None):
        # الإشعار مابيتعدّلش — يتمسح (الفلوس ترجع الخزنة) ويتعمل من جديد.
        return obj is None and super().has_change_permission(request, obj)

    def customer(self, obj):
        return obj.order.customer
    customer.short_description = _("العميل")

    def total_display(self, obj):
        return _amount_html(obj.total, '#dc2626')
    total_display.short_description = _("الإجمالي")


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
        (_('🏭 الأصل'), {'fields': ('name', 'category', 'machine', 'notes')}),
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
    fields = ('treasury', 'transaction_type', 'amount', 'description', 'date')
    verbose_name = _("سداد")
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
    actions = ['cancel_selected']
    fieldsets = (
        (_('🧾 الفاتورة'), {
            'fields': ('supplier', 'supplier_bill_no', 'date', 'due_date', 'category', 'description'),
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
