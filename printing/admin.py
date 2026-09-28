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


@admin.register(PrintCustomer)
class PrintCustomerAdmin(PrintSecureAdmin):
    list_display = ('name', 'company', 'phone', 'whatsapp', 'orders_count', 'statement_link')
    search_fields = ('name', 'company', 'phone')
    list_filter = ('created_at',)

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
    fields = ('product_type_text', 'description', 'machine', 'paper_size', 'quantity', 'copies', 'unit_price', 'total_price', 'design_file', 'is_complete')
    autocomplete_fields = ['product_type']


class PrintOrderPaymentInline(admin.TabularInline):
    """💵 Record payments / refunds straight from the order page."""
    model = PrintTransaction
    form = PrintTransactionForm
    extra = 1
    fields = ('treasury', 'transaction_type', 'amount', 'description', 'date')
    verbose_name = "دفعة / استرداد"
    verbose_name_plural = "💵 الدفعات على الطلب (إيداع = دفعة من العميل، سحب = استرداد له)"


@admin.register(PrintOrder)
class PrintOrderAdmin(PrintSecureAdmin):
    list_display = ('order_number', 'customer', 'status_badge', 'total_display', 'paid_display', 'remaining_display', 'profit_badge', 'has_files_badge', 'date_created')
    list_filter = ('status', 'branch', 'date_created')
    search_fields = ('order_number', 'customer__name')
    list_select_related = ('customer', 'branch')
    date_hierarchy = 'date_created'
    inlines = [PrintJobInline, PrintOrderPaymentInline]
    readonly_fields = ('paid_amount', 'date_delivered')

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
        form.instance.recalc_total_from_jobs()
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


@admin.register(PrintJob)
class PrintJobAdmin(PrintSecureAdmin):
    list_display = ('description', 'product_type_badge', 'order', 'machine', 'quantity', 'total_price', 'cost_display', 'profit_display', 'is_complete')
    list_filter = ('is_complete', 'machine', 'product_type', 'paper_size')
    search_fields = ('description', 'product_type_text')
    list_select_related = ('order', 'machine', 'product_type')
    autocomplete_fields = ['product_type']
    inlines = [PrintJobMaterialInline]

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
    list_display = ('name', 'category', 'quantity_display', 'cost_per_unit', 'stock_value_display', 'stock_alert')
    list_filter = ('category', 'branch')
    search_fields = ('name', 'sku')
    list_select_related = ('branch',)

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
    balance_display.short_description = "الرصيد"


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
