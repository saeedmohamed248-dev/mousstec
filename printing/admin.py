"""
🎨 Mousstec Printing Admin — لوحة تحكم المطابع والتصميم
"""
from django.contrib import admin
from django.utils.html import format_html
from erp_core.localization import current_tenant_symbol as _cur_sym
from django.db.models import Sum, Count, Avg
from django.utils import timezone
from django.db import connection
from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError

from . import ledger
from .models import (
    PrintBranch, PrintCustomer, MachineProfile, Designer,
    DesignerWorkLog, PrintOrder, PrintJob, PrintMaterial, PrintJobMaterial,
    PrintTreasury, PrintTransaction, ProductType, StaffPermission,
    PriceQuotation, QuotationLine,
    PrintSupplier, MaterialPurchase, MaterialPurchaseLine, PrintStockAdjustment,
    PrintProof, PrintCreditAllocation,
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
            raise ValidationError(f"رصيد الخزنة «{treasury.name}» لا يكفي: المتاح {available:,.2f}.")
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
    verbose_name = "دفعة على الحساب"
    verbose_name_plural = "💳 دفعات على حساب العميل (رصيد مقدّم — إيداع = دفعة، سحب = رد فلوس له)"

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
        label = 'عليه' if bal > 0 else ('ليه' if bal < 0 else '')
        return format_html('<b style="color:{};">{} {}</b> <small>{}</small>', color,
                           f"{float(abs(bal)):,.2f}", _cur_sym(), label)
    balance_display.short_description = "الرصيد"

    def credit_display(self, obj):
        credit = obj.account_summary()['credit']
        return _money(credit) if credit else '-'
    credit_display.short_description = "رصيد مقدّم متاح"

    @admin.action(description="💳 سدّد الطلبات المفتوحة من الرصيد المقدّم")
    def settle_open_orders_from_credit(self, request, queryset):
        total = 0
        for customer in queryset:
            for order in customer.printorder_set.exclude(status__in=PrintOrder.NON_DEBT_STATUSES).order_by('date_created'):
                try:
                    total += PrintCreditAllocation.apply_available(order, user=request.user)
                except ValidationError as exc:
                    messages.error(request, f"#{order.order_number}: {'؛ '.join(exc.messages)}")
        messages.success(request, f"اتسدّد {total:,.2f} من الأرصدة المقدّمة.")

    def orders_count(self, obj):
        count = obj.printorder_set.count()
        return format_html('<b>{}</b>', count)
    orders_count.short_description = "عدد الطلبات"

    def statement_link(self, obj):
        return format_html(
            '<a href="/printing/customer/{}/statement/" target="_blank" '
            'style="background:linear-gradient(135deg,#ec4899,#8b5cf6); color:#fff; '
            'padding:5px 12px; border-radius:8px; text-decoration:none; font-weight:700; font-size:0.82rem;">'
            '📒 كشف حساب</a>', obj.pk)
    statement_link.short_description = "كشف حساب"


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
        ('📋 بيانات الماكينة', {
            'fields': ('name', 'machine_type', 'brand', 'model_number', 'branch', 'is_active')
        }),
        ('⚡ تكاليف التشغيل', {
            'fields': ('power_consumption_kwh', 'electricity_rate_per_kwh', 'hourly_labor_cost'),
            'description': 'أدخل بيانات الاستهلاك لحساب تكلفة التشغيل تلقائياً'
        }),
        ('🎨 تكلفة الأحبار (CMYK)', {
            'fields': ('ink_cyan_cost_per_ml', 'ink_magenta_cost_per_ml', 'ink_yellow_cost_per_ml', 'ink_black_cost_per_ml'),
            'classes': ('collapse',)
        }),
        ('📊 الصيانة والإحصائيات', {
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
    machine_type_badge.short_description = "النوع"

    def hourly_cost_display(self, obj):
        cost = obj.hourly_operating_cost
        return format_html('<b style="color:#f59e0b;">{} {}/ساعة</b>', f"{float(cost):,.2f}", _cur_sym())
    hourly_cost_display.short_description = "تكلفة التشغيل"

    def status_badge(self, obj):
        if obj.is_active:
            return format_html('<span style="color:#10b981;font-weight:bold;">🟢 تعمل</span>')
        return format_html('<span style="color:#ef4444;font-weight:bold;">🔴 متوقفة</span>')
    status_badge.short_description = "الحالة"


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
    month_works.short_description = "أعمال الشهر"

    def month_hours(self, obj):
        stats = obj.get_month_stats()
        h = stats.get('total_hours') or 0
        return format_html('<b>{}</b> ساعة', f"{float(h):.1f}")
    month_hours.short_description = "ساعات الشهر"

    def avg_rating(self, obj):
        stats = obj.get_month_stats()
        r = stats.get('avg_rating')
        if not r:
            return '-'
        stars = '⭐' * int(round(r))
        return format_html('<span title="{}">{}</span>', f"{r:.1f}", stars)
    avg_rating.short_description = "التقييم"


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
    execution_badge.short_description = "نوع التنفيذ"

    def rating_display(self, obj):
        if obj.client_rating:
            return '⭐' * obj.client_rating
        return '-'
    rating_display.short_description = "التقييم"


# =====================================================================
# 📋 طلبات ومهام الطباعة
# =====================================================================

class PrintJobInline(admin.TabularInline):
    model = PrintJob
    extra = 1
    fields = ('product_type_text', 'description', 'machine', 'paper_size', 'quantity', 'copies', 'unit_price', 'total_price', 'design_file', 'stage', 'proof_waived', 'is_complete')
    readonly_fields = ('total_price',)
    autocomplete_fields = ['product_type']


class PrintOrderPaymentInline(admin.TabularInline):
    """💵 Record payments / refunds straight from the order page."""
    model = PrintTransaction
    form = PrintTransactionForm
    fk_name = 'order'
    extra = 1
    fields = ('treasury', 'transaction_type', 'amount', 'description', 'date')
    verbose_name = "دفعة / استرداد"
    verbose_name_plural = "💵 الدفعات على الطلب (إيداع = دفعة من العميل، سحب = استرداد له)"


class PrintCreditAllocationInline(admin.TabularInline):
    """💳 تسديد من الرصيد المقدّم على حساب العميل — من غير حركة خزنة."""
    model = PrintCreditAllocation
    extra = 0
    fields = ('amount', 'date', 'notes')
    verbose_name = "تسديد من الرصيد المقدّم"
    verbose_name_plural = "💳 تسديد من الرصيد المقدّم على حساب العميل (من غير ما الفلوس تتحرك في الخزنة)"


@admin.register(PrintOrder)
class PrintOrderAdmin(PrintSecureAdmin):
    list_display = ('order_number', 'customer', 'status_badge', 'production_badge', 'total_display', 'paid_display', 'remaining_display', 'profit_badge', 'has_files_badge', 'date_created')
    list_filter = ('status', 'branch', 'date_created')
    search_fields = ('order_number', 'customer__name')
    list_select_related = ('customer', 'branch')
    date_hierarchy = 'date_created'
    inlines = [PrintJobInline, PrintOrderPaymentInline, PrintCreditAllocationInline]
    readonly_fields = ('paid_amount', 'date_delivered')
    actions = ['apply_customer_credit']

    @admin.action(description="💳 سدّد من رصيد العميل المقدّم")
    def apply_customer_credit(self, request, queryset):
        for order in queryset.select_related('customer'):
            try:
                amount = PrintCreditAllocation.apply_available(order, user=request.user)
            except ValidationError as exc:
                messages.error(request, f"#{order.order_number}: {'؛ '.join(exc.messages)}")
                continue
            if amount:
                messages.success(request, f"#{order.order_number}: اتسدّد {amount:,.2f} من الرصيد المقدّم.")
            else:
                messages.info(request, f"#{order.order_number}: مفيش رصيد مقدّم متاح أو الطلب متسدّد.")

    def production_badge(self, obj):
        stages = list(obj.jobs.values_list('stage', flat=True))
        if not stages:
            return '-'
        done = sum(1 for st in stages if st == 'done')
        waiting = sum(1 for st in stages if st == 'proof')
        extra = f' · ⏳ {waiting} بروفة' if waiting else ''
        return format_html('<span style="font-weight:700;">{}/{} خلصت{}</span>', done, len(stages), extra)
    production_badge.short_description = "الإنتاج"

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
        order.refresh_production_status()
        # 💳 حد الائتمان: الطلب اللي اتأكد والعميل عدّى الحد بيرجع مسودة.
        hold = order.apply_credit_hold(previous_status=getattr(order, '_previous_status', None))
        if hold:
            messages.warning(request, hold)
        ledger.on_order_changed(order.pk)
    fieldsets = (
        ('📋 بيانات الطلب', {
            'fields': ('order_number', 'customer', 'branch', 'status', 'date_due', 'date_delivered'),
        }),
        ('💰 المالي', {
            'fields': ('total_amount', 'discount', 'tax_amount', 'paid_amount'),
            'description': ('الإجمالي بيتحسب تلقائياً من أسعار المهام (لو فيه مهام مسعّرة). '
                            'المدفوع بيتحسب من الدفعات تحت — سجّل الدفعة في جدول «الدفعات على الطلب».'),
        }),
        ('📁 ملفات المشروع', {
            'fields': ('project_file', 'project_file_2', 'project_file_3'),
            'description': 'ارفع ملفات المشروع الأصلية (PSD, AI, PDF, إلخ) — يتم حفظها بأمان على السيرفر',
        }),
        ('📝 ملاحظات', {
            'fields': ('notes',),
            'classes': ('collapse',),
        }),
    )

    def has_files_badge(self, obj):
        count = sum(1 for f in [obj.project_file, obj.project_file_2, obj.project_file_3] if f)
        if count:
            return format_html('<span style="background:#8b5cf6;color:white;padding:2px 8px;border-radius:8px;font-size:11px;font-weight:bold;">📁 {}</span>', count)
        return '-'
    has_files_badge.short_description = "ملفات"

    def status_badge(self, obj):
        colors = {
            'draft': '#94a3b8', 'confirmed': '#3b82f6', 'in_progress': '#f59e0b',
            'ready': '#8b5cf6', 'delivered': '#10b981', 'cancelled': '#ef4444',
        }
        c = colors.get(obj.status, '#64748b')
        return format_html('<span style="background:{};color:white;padding:3px 10px;border-radius:12px;font-size:11px;font-weight:700;">{}</span>', c, obj.get_status_display())
    status_badge.short_description = "الحالة"

    def total_display(self, obj):
        return format_html('<b>{}</b> {}', f"{float(obj.net_total):,.2f}", _cur_sym())
    total_display.short_description = "الإجمالي"

    def paid_display(self, obj):
        return format_html('<span style="color:#10b981;font-weight:bold;">{}</span> {}', f"{float(obj.paid_amount):,.2f}", _cur_sym())
    paid_display.short_description = "المدفوع"

    def remaining_display(self, obj):
        r = obj.remaining
        color = '#ef4444' if r > 0 else '#10b981'
        return format_html('<span style="color:{};font-weight:bold;">{}</span> {}', color, f"{float(r):,.2f}", _cur_sym())
    remaining_display.short_description = "المتبقي"

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
            'title="افتح تحليل الربحية">{} {} {} ({}%)</a>',
            obj.pk, bg, icon, f"{float(profit):,.0f}", _cur_sym(), f"{float(margin):.1f}",
        )
    profit_badge.short_description = "الربح"


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
                    f"المخزون من «{material.name}» مش كفاية: المتاح {available} {material.unit}.")
        return data


class PrintJobMaterialInline(admin.TabularInline):
    """🧾 Materials used on this job — deducted from stock, added to its cost."""
    model = PrintJobMaterial
    form = PrintJobMaterialForm
    extra = 1
    fields = ('material', 'quantity', 'unit_cost')
    readonly_fields = ('unit_cost',)
    verbose_name = "خامة مصروفة"
    verbose_name_plural = "🧾 الخامات المصروفة (بتتخصم من المخزون وتدخل في التكلفة)"


class PrintProofInline(admin.TabularInline):
    """🖼️ بروفات المهمة — ارفع البروفة وابعت الرابط للعميل يوافق أو يطلب تعديل."""
    model = PrintProof
    extra = 0
    fields = ('version', 'file', 'notes', 'status', 'customer_comment', 'share_links')
    readonly_fields = ('version', 'status', 'customer_comment', 'share_links')
    verbose_name = "بروفة"
    verbose_name_plural = "🖼️ البروفات (العميل بيوافق من الرابط — الطباعة مستنية موافقته)"

    def share_links(self, obj):
        if not obj or not obj.pk:
            return '-'
        return _proof_links(obj)
    share_links.short_description = "رابط العميل"


def _proof_links(proof):
    customer = proof.job.order.customer
    number = ''.join(ch for ch in (customer.whatsapp or customer.phone or '') if ch.isdigit())
    if number.startswith('0'):
        number = '2' + number
    from urllib.parse import quote as _urlquote
    msg = _urlquote(f"بروفة «{proof.job.description[:60]}» — راجعها ووافق أو اطلب تعديل: "
                    f"/printing/proof/{proof.share_token}/")
    return format_html(
        '<a href="/printing/proof/{}/" target="_blank" style="font-weight:700;">🔗 فتح</a> · '
        '<a href="https://wa.me/{}?text={}" target="_blank" style="color:#16a34a;font-weight:700;">واتساب</a>',
        proof.share_token, number, msg)


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
    readonly_fields = ('total_price',)

    def stage_badge(self, obj):
        return format_html('<span style="background:{};color:white;padding:3px 10px;border-radius:12px;'
                           'font-size:11px;font-weight:700;">{}</span>',
                           STAGE_COLORS.get(obj.stage, '#64748b'), obj.get_stage_display())
    stage_badge.short_description = "المرحلة"

    @admin.action(description="➡️ انقل للمرحلة اللي بعدها (تصميم ← بروفة ← طباعة ← تشطيب ← خلصت)")
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
    product_type_badge.short_description = "نوع البند"

    def cost_display(self, obj):
        cost = obj.calculated_cost
        return format_html('<span style="color:#f59e0b;">{}</span> {}', f"{float(cost):,.2f}", _cur_sym())
    cost_display.short_description = "التكلفة الفعلية"

    def profit_display(self, obj):
        p = obj.profit
        color = '#10b981' if p >= 0 else '#ef4444'
        return format_html('<span style="color:{};font-weight:bold;">{}</span> {}', color, f"{float(p):,.2f}", _cur_sym())
    profit_display.short_description = "الربح"


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
            '<a href="/secure-portal/printing/materialpurchase/add/">📥 فاتورة شراء</a> · '
            '<a href="/secure-portal/printing/printstockadjustment/add/?material={}">⚖️ تسوية جرد / تالف</a>',
            obj.pk)
    stock_moves.short_description = "تحريك المخزون"

    def cost_display(self, obj):
        return f"{float(obj.cost_per_unit):,.4f}".rstrip('0').rstrip('.')
    cost_display.short_description = "تكلفة الوحدة"

    def quantity_display(self, obj):
        return format_html('<b>{}</b> {}', f"{float(obj.quantity):,.1f}", obj.unit)
    quantity_display.short_description = "الكمية"

    def stock_value_display(self, obj):
        return format_html('<b>{}</b> {}', f"{float(obj.stock_value):,.2f}", _cur_sym())
    stock_value_display.short_description = "القيمة"

    def stock_alert(self, obj):
        if obj.is_low_stock:
            return format_html('<span style="background:#ef4444;color:white;padding:2px 8px;border-radius:8px;font-size:11px;font-weight:bold;">⚠️ منخفض</span>')
        return format_html('<span style="color:#10b981;">✅ متوفر</span>')
    stock_alert.short_description = "المخزون"


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
    balance_display.short_description = "الرصيد"


@admin.register(PrintTransaction)
class PrintTransactionAdmin(PrintSecureAdmin):
    form = PrintTransactionForm
    list_display = ('type_badge', 'amount_display', 'category', 'treasury', 'party', 'description', 'date')
    list_filter = ('transaction_type', 'category', 'treasury', 'date')
    search_fields = ('description', 'order__order_number', 'customer__name', 'supplier__name')
    list_select_related = ('treasury', 'order', 'customer', 'supplier', 'purchase')
    exclude = ('created_by',)

    def party(self, obj):
        if obj.order_id:
            return f"#{obj.order.order_number}"
        return obj.customer or obj.supplier or '-'
    party.short_description = "مرتبطة بـ"


    def save_model(self, request, obj, form, change):
        if not obj.created_by_id:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)

    def type_badge(self, obj):
        if obj.transaction_type == 'in':
            return format_html('<span style="color:#10b981;font-weight:bold;">🟢 إيداع</span>')
        return format_html('<span style="color:#ef4444;font-weight:bold;">🔴 مصروف</span>')
    type_badge.short_description = "النوع"

    def amount_display(self, obj):
        color = '#10b981' if obj.transaction_type == 'in' else '#ef4444'
        return format_html('<b style="color:{};">{} {}</b>', color, f"{float(obj.amount):,.2f}", _cur_sym())
    amount_display.short_description = "المبلغ"


# =====================================================================
# 🏷️ أنواع البنود
# =====================================================================

@admin.register(ProductType)
class ProductTypeAdmin(PrintSecureAdmin):
    list_display = ('name', 'usage_count_display', 'created_at')
    search_fields = ('name',)
    ordering = ('-usage_count',)

    def usage_count_display(self, obj):
        return format_html('<b style="color:#6366f1;">{}</b> مرة', obj.usage_count)
    usage_count_display.short_description = "عدد الاستخدام"


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
        ('👤 الموظف', {'fields': ('user',)}),
        ('💰 الصلاحيات المالية', {
            'fields': ('can_view_treasury', 'can_manage_treasury', 'can_view_profits'),
            'description': '⚠️ صلاحيات حساسة — فقط للمديرين والمحاسبين',
        }),
        ('📋 الطلبات', {
            'fields': ('can_create_orders', 'can_edit_orders', 'can_delete_orders', 'can_view_all_orders'),
        }),
        ('📁 الملفات والعملاء', {
            'fields': ('can_view_project_files', 'can_upload_project_files', 'can_manage_customers'),
        }),
        ('📦 المخزون والمصممين', {
            'fields': ('can_manage_stock', 'can_view_designers'),
        }),
        ('🤖 AI وتقارير', {
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
    readonly_fields = ('quote_number', 'share_token', 'subtotal', 'total', 'sent_at', 'responded_at', 'converted_order')
    inlines = [QuotationLineInline]
    actions = ['convert_to_order']

    @admin.action(description="🔁 تحويل العروض المقبولة لطلبات طباعة (مهمة لكل بند)")
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
            messages.success(request, f"اتعمل {len(done)} طلب: {', '.join(done)}")
        for f in failed:
            messages.warning(request, f)
    fieldsets = (
        ('بيانات العميل', {
            'fields': ('customer', 'customer_name', 'customer_phone', 'customer_whatsapp')
        }),
        ('محتوى العرض', {
            'fields': ('title', 'notes', 'discount', 'tax_percent')
        }),
        ('الحالة والصلاحية', {
            'fields': ('status', 'valid_until', 'sent_at', 'responded_at', 'converted_order')
        }),
        ('المراجع', {
            'classes': ('collapse',),
            'fields': ('quote_number', 'share_token', 'subtotal', 'total', 'created_by')
        }),
    )

    def customer_col(self, obj):
        return obj.customer_display
    customer_col.short_description = "العميل"

    def total_col(self, obj):
        return format_html('<b style="color:#ec4899; font-size:1rem;">{} {}</b>', f'{obj.total:,.2f}', _cur_sym())
    total_col.short_description = "الإجمالي"

    def status_badge(self, obj):
        colors = {
            'draft': '#94a3b8', 'sent': '#f59e0b', 'accepted': '#10b981',
            'rejected': '#ef4444', 'expired': '#64748b', 'converted': '#8b5cf6',
        }
        c = colors.get(obj.status, '#94a3b8')
        return format_html(
            '<span style="background:{}; color:#fff; padding:3px 10px; border-radius:999px; '
            'font-size:0.78rem; font-weight:700;">{}</span>',
            c, obj.get_status_display())
    status_badge.short_description = "الحالة"

    def share_link(self, obj):
        return format_html(
            '<a href="/printing/quotation/view/{}/" target="_blank" '
            'style="background:linear-gradient(135deg,#ec4899,#8b5cf6); color:#fff; '
            'padding:4px 12px; border-radius:8px; text-decoration:none; font-weight:700; font-size:0.78rem;">'
            '🔗 فتح للعميل</a>', obj.share_token)
    share_link.short_description = "رابط العميل"

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
    verbose_name = "دفعة على حساب المورد"
    verbose_name_plural = "💸 دفعات على حساب المورد (سحب = دفعنا له، إيداع = رجّعلنا فلوس)"

    def get_queryset(self, request):
        return super().get_queryset(request).filter(purchase__isnull=True)


@admin.register(PrintSupplier)
class PrintSupplierAdmin(_CreatedByFormsetMixin, PrintSecureAdmin):
    list_display = ('name', 'company', 'phone', 'balance_display', 'is_active', 'statement_link')
    list_filter = ('is_active',)
    search_fields = ('name', 'company', 'phone')
    inlines = [SupplierPaymentInline]

    def balance_display(self, obj):
        bal = obj.balance
        color = '#ef4444' if bal > 0 else '#10b981'
        label = 'علينا' if bal > 0 else ('لينا' if bal < 0 else '')
        return format_html('<b style="color:{};">{} {}</b> <small>{}</small>', color,
                           f"{float(abs(bal)):,.2f}", _cur_sym(), label)
    balance_display.short_description = "الرصيد"

    def statement_link(self, obj):
        return format_html(
            '<a href="/printing/supplier/{}/statement/" target="_blank" '
            'style="background:linear-gradient(135deg,#0ea5e9,#8b5cf6); color:#fff; '
            'padding:5px 12px; border-radius:8px; text-decoration:none; font-weight:700; font-size:0.82rem;">'
            '📒 كشف حساب</a>', obj.pk)
    statement_link.short_description = "كشف حساب"


class MaterialPurchaseForm(forms.ModelForm):
    receive_now = forms.BooleanField(
        required=False, label="📥 استلم البضاعة دلوقتي",
        help_text="الخامات تدخل المخزن بمتوسط التكلفة، ولو الدفع نقدي المبلغ يتخصم من الخزنة لوحده.")

    class Meta:
        model = MaterialPurchase
        fields = '__all__'

    def clean(self):
        data = super().clean()
        if data.get('receive_now') and data.get('payment_terms') == 'cash' and not data.get('treasury'):
            raise ValidationError("اختار الخزنة اللي هيتصرف منها (أو خلّي الدفع آجل).")
        return data


class MaterialPurchaseLineInline(admin.TabularInline):
    model = MaterialPurchaseLine
    extra = 1
    fields = ('material', 'quantity', 'unit_cost', 'line_total')
    readonly_fields = ('line_total',)
    verbose_name = "بند خامة"
    verbose_name_plural = "📦 بنود الفاتورة (الخامات)"

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
    verbose_name = "دفعة"
    verbose_name_plural = "💸 مدفوعات الفاتورة (سحب = دفعنا للمورد، إيداع = استرداد منه)"


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
        ('🧾 بيانات الفاتورة', {
            'fields': ('supplier', 'supplier_invoice_no', 'branch', 'date', 'notes'),
        }),
        ('💰 الدفع والإجمالي', {
            'fields': ('payment_terms', 'treasury', 'discount', 'tax_amount', 'totals', 'receive_now'),
            'description': 'نقدي: الفلوس بتطلع من الخزنة لوحدها وقت الاستلام. آجل: المبلغ بيفضل على حساب المورد.',
        }),
    )

    def get_readonly_fields(self, request, obj=None):
        ro = ['totals']
        if obj and obj.status != 'draft':
            ro += ['supplier', 'branch', 'date', 'payment_terms', 'treasury', 'discount', 'tax_amount']
        return ro

    def get_fieldsets(self, request, obj=None):
        fieldsets = super().get_fieldsets(request, obj)
        if obj and obj.status != 'draft':
            fieldsets = [(name, dict(opts, fields=tuple(f for f in opts['fields'] if f != 'receive_now')))
                         for name, opts in fieldsets]
        return fieldsets

    def has_delete_permission(self, request, obj=None):
        if obj is not None and obj.status == 'received':
            return False
        return super().has_delete_permission(request, obj)

    def delete_queryset(self, request, queryset):
        received = queryset.filter(status='received')
        if received.exists():
            messages.warning(request, "الفواتير المستلمة مابتتمسحش — الغيها الأول: "
                             + ', '.join(received.values_list('number', flat=True)))
        super().delete_queryset(request, queryset.exclude(status='received'))

    def totals(self, obj):
        if not obj or not obj.pk:
            return '-'
        return format_html(
            'البنود {} − خصم {} + ضريبة {} = <b>{}</b> · المدفوع {} · الباقي <b>{}</b> · الحالة: <b>{}</b>',
            f"{obj.total_amount:,.2f}", f"{obj.discount:,.2f}", f"{obj.tax_amount:,.2f}",
            f"{obj.net_total:,.2f}", f"{obj.paid_amount:,.2f}", f"{obj.remaining:,.2f}",
            obj.get_status_display())
    totals.short_description = "الإجمالي"

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
        paid = "واتدفعت من الخزنة" if purchase.payment_terms == 'cash' else "وعلى حساب المورد (آجل)"
        messages.success(request, f"#{purchase.number}: الخامات دخلت المخزن {paid}.")
        return True

    @admin.action(description="📥 استلم الفواتير المحددة (تدخل المخزن وتتدفع لو نقدي)")
    def receive_selected(self, request, queryset):
        for purchase in queryset.filter(status='draft'):
            self._receive(request, purchase)

    @admin.action(description="↩️ الغي الفواتير المحددة (الخامات ترجع للمورد)")
    def cancel_selected(self, request, queryset):
        for purchase in queryset.exclude(status='cancelled'):
            try:
                purchase.cancel(user=request.user)
            except ValidationError as exc:
                messages.error(request, f"#{purchase.number}: {'؛ '.join(exc.messages)}")
                continue
            note = (f" — المدفوع {purchase.paid_amount:,.2f} بقى رصيد لينا عند المورد"
                    if purchase.paid_amount > 0 else '')
            messages.success(request, f"#{purchase.number} اتلغت{note}.")

    def status_badge(self, obj):
        return format_html('<span style="background:{};color:white;padding:3px 10px;border-radius:12px;'
                           'font-size:11px;font-weight:700;">{}</span>',
                           PURCHASE_STATUS_COLORS.get(obj.status, '#64748b'), obj.get_status_display())
    status_badge.short_description = "الحالة"

    def net_display(self, obj):
        return _money(obj.net_total)
    net_display.short_description = "الإجمالي"

    def paid_display(self, obj):
        return _money(obj.paid_amount)
    paid_display.short_description = "المدفوع"

    def remaining_display(self, obj):
        r = obj.remaining
        color = '#ef4444' if r > 0 else '#10b981'
        return format_html('<b style="color:{};">{}</b> {}', color, f"{float(r):,.2f}", _cur_sym())
    remaining_display.short_description = "الباقي"


# =====================================================================
# ⚖️ تسويات المخزن
# =====================================================================

class PrintStockAdjustmentForm(forms.ModelForm):
    counted_quantity = forms.DecimalField(
        required=False, max_digits=12, decimal_places=2, label="الكمية الفعلية بعد الجرد",
        help_text="لو عملت جرد اكتب الموجود فعلاً — السيستم يحسب الفرق لوحده.")

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
            raise ValidationError("اكتب الكمية الفعلية بعد الجرد أو التغيير في الكمية (ومينفعش يبقى صفر).")
        if material is not None and material.quantity + change < 0:
            raise ValidationError(f"المتاح من «{material.name}» {material.quantity} {material.unit} بس.")
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
    change_display.short_description = "التغيير"

    def value_display(self, obj):
        return _money(obj.value)
    value_display.short_description = "القيمة"


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
    status_badge.short_description = "الحالة"

    def links(self, obj):
        return _proof_links(obj) if obj and obj.pk else '-'
    links.short_description = "رابط العميل"

    def _respond(self, request, queryset, action):
        for proof in queryset:
            try:
                proof.respond(action, f"سجّلها {request.user.get_username()} (موافقة بالتليفون/واتساب)"
                              if action == 'approve' else "")
                messages.success(request, f"{proof}: {proof.get_status_display()}")
            except ValidationError as exc:
                messages.warning(request, f"{proof}: {'؛ '.join(exc.messages)}")

    @admin.action(description="✅ العميل وافق (بالتليفون/واتساب) — ابدأ الطباعة")
    def approve_on_behalf(self, request, queryset):
        self._respond(request, queryset, 'approve')

    @admin.action(description="✏️ العميل طلب تعديل — رجّع المهمة للتصميم")
    def request_changes_on_behalf(self, request, queryset):
        self._respond(request, queryset, 'reject')
