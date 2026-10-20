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

from .models import (
    PrintBranch, PrintCustomer, MachineProfile, Designer,
    DesignerWorkLog, PrintOrder, PrintJob, PrintMaterial, PrintJobMaterial,
    PrintTreasury, PrintTransaction, ProductType, StaffPermission,
    PriceQuotation, QuotationLine,
)


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


@admin.register(PrintCustomer)
class PrintCustomerAdmin(PrintSecureAdmin):
    list_display = ('name', 'company', 'phone', 'whatsapp', 'orders_count', 'statement_link')
    search_fields = ('name', 'company', 'phone')
    list_filter = ('created_at',)

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


class PrintJobInline(admin.TabularInline):
    model = PrintJob
    extra = 1
    fields = ('product_type_text', 'description', 'machine', 'paper_size', 'quantity', 'copies', 'unit_price', 'total_price', 'design_file', 'is_complete')
    # 🐛 [FIX]: الإجمالي كان خانة بتتكتب — وبعدين بيتحسب من جديد (سعر × كمية × نسخ)
    # فاللي اتكتب بيضيع من غير ما حد يعرف. بقى للعرض بس.
    readonly_fields = ('total_price',)
    autocomplete_fields = ['product_type']


class PrintOrderPaymentInline(admin.TabularInline):
    """💵 Record payments / refunds straight from the order page."""
    model = PrintTransaction
    form = PrintTransactionForm
    extra = 1
    fields = ('treasury', 'transaction_type', 'amount', 'description', 'date')
    verbose_name = _("دفعة / استرداد")
    verbose_name_plural = _("💵 الدفعات على الطلب (إيداع = دفعة من العميل، سحب = استرداد له)")


@admin.register(PrintOrder)
class PrintOrderAdmin(PrintSecureAdmin):
    form = PrintOrderForm
    list_display = ('order_number', 'customer', 'status_badge', 'total_display', 'paid_display', 'remaining_display', 'profit_badge', 'has_files_badge', 'due_display', 'date_created')
    list_filter = ('status', 'branch', 'date_created')
    search_fields = ('order_number', 'customer__name', 'customer__phone')
    list_select_related = ('customer', 'branch')
    date_hierarchy = 'date_created'
    inlines = [PrintJobInline, PrintOrderPaymentInline]
    # 🐛 [FIX]: رقم الطلب كان مطلوب يتكتب بإيدك في «طلب جديد» — فالترقيم
    # التلقائي PO-YYMMDD-NNN عمره ما اشتغل من الأدمن. دلوقتي بيتولّد لوحده.
    readonly_fields = ('order_number', 'order_actions', 'paid_amount', 'date_delivered')
    actions = ['cancel_and_refund']

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
                self.message_user(request, gettext("#%(no)s اتسلّم بالفعل — لو العميل رجّعه سجّل الاسترداد يدوي من صفحة الطلب.")
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

    def get_readonly_fields(self, request, obj=None):
        ro = list(super().get_readonly_fields(request, obj))
        # Priced jobs drive the total (see PrintOrder.recalc_total_from_jobs).
        if obj and obj.pk and obj.jobs.filter(total_price__gt=0).exists():
            ro.append('total_amount')
        return ro

    def save_formset(self, request, form, formset, change):
        instances = formset.save(commit=False)
        for inst in instances:
            if isinstance(inst, PrintTransaction) and not inst.created_by_id:
                inst.created_by = request.user
            inst.save()
        for obj in formset.deleted_objects:
            obj.delete()
        formset.save_m2m()

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        order = form.instance
        order.recalc_total_from_jobs()
        moved = order.sync_status_from_jobs()
        if moved:
            self.message_user(request, gettext("حالة الطلب بقت «%(status)s» حسب المهام.")
                              % {'status': order.get_status_display()}, messages.INFO)
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
        remaining = order.net_total - paid
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


@admin.register(PrintJob)
class PrintJobAdmin(PrintSecureAdmin):
    list_display = ('description', 'product_type_badge', 'order', 'machine', 'quantity', 'total_price', 'cost_display', 'profit_display', 'is_complete')
    list_filter = ('is_complete', 'machine', 'product_type', 'paper_size')
    search_fields = ('description', 'product_type_text')
    list_select_related = ('order', 'machine', 'product_type')
    autocomplete_fields = ['product_type']
    inlines = [PrintJobMaterialInline]
    readonly_fields = ('total_price', 'completed_at')

    def save_formset(self, request, form, formset, change):
        # Stock shortage is a ValidationError from the model — show it on the
        # page instead of a server error.
        try:
            super().save_formset(request, form, formset, change)
        except ValidationError as exc:
            from django.contrib import messages
            messages.error(request, '؛ '.join(exc.messages))

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
    list_display = ('name', 'category', 'quantity_display', 'cost_per_unit', 'stock_value_display', 'stock_alert')
    list_filter = ('category', 'branch')
    search_fields = ('name', 'sku')
    list_select_related = ('branch',)

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
    fields = ('transaction_type', 'amount', 'description', 'date')


@admin.register(PrintTreasury)
class PrintTreasuryAdmin(PrintSecureAdmin):
    list_display = ('name', 'branch', 'balance_display', 'is_active')
    inlines = [PrintTransactionInline]

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
            obj.delete()
        formset.save_m2m()

    def balance_display(self, obj):
        color = '#10b981' if obj.balance >= 0 else '#ef4444'
        return format_html('<b style="color:{};">{} {}</b>', color, f"{float(obj.balance):,.2f}", _cur_sym())
    balance_display.short_description = _("الرصيد")


@admin.register(PrintTransaction)
class PrintTransactionAdmin(PrintSecureAdmin):
    form = PrintTransactionForm
    list_display = ('type_badge', 'amount_display', 'treasury', 'order', 'description', 'date')
    list_filter = ('transaction_type', 'treasury', 'date')
    exclude = ('created_by',)

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
