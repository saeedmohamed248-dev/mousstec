"""🖨️ لوحة المطابع — الماكينات، المصممين، الطلبات والمهام، أنواع البنود."""
from decimal import Decimal

from django.contrib import admin, messages
from django.urls import reverse
from django.utils.html import format_html
from django.utils.translation import gettext, gettext_lazy as _
from erp_core.localization import current_tenant_symbol as _cur_sym
from django.db.models import Sum
from django.db import models, transaction
from django.utils import timezone
from django.db import connection
from django import forms
from django.core.exceptions import ValidationError

from .. import ledger
from ..models import (
    MachineProfile, Designer,
    DesignerWorkLog, PrintOrder, PrintJob, PrintJobMaterial,
    PrintTransaction, ProductType, PrintProof, PrintCreditAllocation, PrintCreditNote, PrintCheque,
)
from ._common import PrintSecureAdmin, PrintTransactionForm, _CreatedByFormsetMixin
from .customers import _money_txns


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
    # 🧾 الضريبة كانت مبلغ بس — الموظف بيحسب الـ ١٤٪ بإيده. النسبة بتتحوّل مبلغ بعد حساب إجمالي المهام.
    tax_rate = forms.DecimalField(required=False, min_value=0, max_value=100, decimal_places=2,
                                  label=_("نسبة الضريبة ٪"),
                                  help_text=_("اكتب ١٤ مثلاً والمبلغ يتحسب لوحده من (الإجمالي − الخصم). فاضية = المبلغ زي ما هو."))

    class Meta:
        model = PrintOrder
        fields = '__all__'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # 🐛 [FIX]: «الإجمالي» كان مطلوب وفيه صفر مع إنه بيتحسب من المهام — كان بيلخبط الموظف.
        if 'total_amount' in self.fields:
            self.fields['total_amount'].required = False
        if not self.instance.pk and 'branch' in self.fields and not self.initial.get('branch'):
            qs = self.fields['branch'].queryset
            if qs.count() == 1:                  # فرع واحد ⇒ يتختار لوحده
                self.initial['branch'] = qs.first().pk

    def clean_total_amount(self):
        value = self.cleaned_data.get('total_amount')
        return value if value is not None else (self.instance.total_amount or Decimal('0'))

    def clean(self):
        data = super().clean()
        total = data.get('total_amount', self.instance.total_amount if self.instance.pk else None)
        discount = data.get('discount')
        # 🐛 [FIX]: الخصم كان ممكن يبقى أكبر من الإجمالي ⇒ إيراد بالسالب ومستحق بالسالب
        if total and discount and discount > total:
            self.add_error('discount', gettext("الخصم (%(discount)s) أكبر من إجمالي الطلب (%(total)s).")
                           % {'discount': f"{discount:,.2f}", 'total': f"{total:,.2f}"})
        # 💎 حد طلبات الطباعة الشهري في الباقة (Lite = 100) — على الطلب الجديد بس
        if not self.instance.pk:
            from tenancy.services.plan_limits import print_orders_block
            blocked = print_orders_block()
            if blocked:
                raise forms.ValidationError(blocked)
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
    fields = ('treasury', 'transaction_type', 'amount', 'withholding_tax', 'description', 'date')
    verbose_name = _("دفعة / استرداد")

    def get_queryset(self, request):
        return _money_txns(super().get_queryset(request))
    verbose_name_plural = _("💵 الدفعات على الطلب (إيداع = دفعة من العميل، سحب = استرداد له)")


class PrintChequeInline(admin.TabularInline):
    """🧾 الشيكات على الطلب/الفاتورة (للعرض — الإضافة والصرف من شاشة «الشيكات»)."""
    model = PrintCheque
    extra = 0
    max_num = 0
    can_delete = False
    fields = ('number', 'bank_name', 'due_date', 'amount', 'status')
    readonly_fields = fields
    verbose_name = _("شيك")
    verbose_name_plural = _("🧾 الشيكات")


class PrintCreditNoteInline(admin.TabularInline):
    """↩️ المرتجعات/المسموحات بعد التسليم (للعرض — الإضافة من زرار «إشعار دائن»)."""
    model = PrintCreditNote
    extra = 0
    max_num = 0
    can_delete = False
    fields = ('number', 'kind', 'date', 'reason', 'amount', 'tax_amount', 'refund_treasury')
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
    inlines = [PrintJobInline, PrintOrderPaymentInline, PrintCreditAllocationInline, PrintChequeInline,
               PrintCreditNoteInline]
    # 🐛 [FIX]: رقم الطلب كان مطلوب يتكتب بإيدك في «طلب جديد» — فالترقيم
    # التلقائي PO-YYMMDD-NNN عمره ما اشتغل من الأدمن. دلوقتي بيتولّد لوحده.
    readonly_fields = ('order_number', 'order_actions', 'paid_amount', 'date_delivered')
    actions = ['cancel_and_refund', 'apply_customer_credit', 'write_off_remaining']

    def get_changeform_initial_data(self, request):
        # 🐛 [FIX]: زرار «أو أنشئ فاتورة جديدة بهذا التصميم» في استوديو التصميم كان بيفتح
        #    /printing/ (صفحة التسويق العامة). دلوقتي بيفتح «طلب جديد» هنا بـ ?ai_session=<id>
        #    والتصميم مكتوب في الملاحظات — نفس اللي «ربط بطلب موجود» بيعمله.
        initial = super().get_changeform_initial_data(request)
        initial.pop('ai_session', None)
        sid = (request.GET.get('ai_session') or '').strip()
        if sid.isdigit():
            from clients.models import AIStudioSession
            from printing.views.utils import _get_tenant
            session = AIStudioSession.objects.filter(pk=int(sid), tenant=_get_tenant()).first()
            if session is not None:
                initial['notes'] = f"🎨 تصميم AI Studio (جلسة #{session.pk}): {session.image_url}"
        return initial

    def get_inlines(self, request, obj):
        # التسديد من الرصيد المقدّم بيتحسب على طلب محفوظ (المتبقّي عليه) — مش في «طلب جديد».
        inlines = list(super().get_inlines(request, obj))
        if obj is None or not obj.pk:
            inlines.remove(PrintCreditAllocationInline)
            inlines.remove(PrintCreditNoteInline)
            inlines.remove(PrintChequeInline)
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

    @admin.action(description=_("✂️ اشطب الباقي على الطلبات المسلّمة (خصم / فروق كسور)"))
    def write_off_remaining(self, request, queryset):
        """الباقي الصغير اللي العميل مش هيدفعه (كسور/خصم تعجيل) بيتقفل بإشعار خصم = مصروف."""
        for order in queryset.filter(status='delivered'):
            left = order.remaining
            if left <= 0:
                continue
            try:
                PrintCreditNote.objects.create(order=order, kind='discount', amount=left, created_by=request.user,
                                               reason=gettext('شطب الباقي (فروق كسور / خصم تعجيل دفع)'))
            except ValidationError as exc:
                messages.error(request, f"#{order.order_number}: {'؛ '.join(exc.messages)}")
                continue
            messages.success(request, gettext("#%(no)s: اتشطب %(a)s.") % {
                'no': order.order_number, 'a': f"{left:,.2f}"})

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
        rate = form.cleaned_data.get('tax_rate') if hasattr(form, 'cleaned_data') else None
        if rate is not None:
            order.refresh_from_db(fields=['total_amount', 'discount'])
            order.tax_amount = ((order.total_amount - order.discount) * rate / 100).quantize(Decimal('0.01'))
            order.save(update_fields=['tax_amount'])
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

    #: 🐛 [FIX]: في «طلب جديد» كانت بتظهر خانات فاضية مالهاش لازمة (رقم الطلب «-»، تاريخ التسليم
    #: الفعلي «-»، إجراءات سريعة «—»، المدفوع 0.00) — بتتشال لحد ما الطلب يتحفظ.
    ADD_HIDDEN = ('order_number', 'date_delivered', 'order_actions', 'paid_amount')

    def add_view(self, request, form_url='', extra_context=None):
        """«+ طلب جديد» ⇒ الفاتورة السريعة (العميل ← البنود بأسعارها ← الحساب ← الدفع في شاشة واحدة).
        الشاشة التفصيلية لسه موجودة بـ ?full=1 (ومن استوديو التصميم والنوافذ المنبثقة)."""
        q = request.GET
        if request.method == 'GET' and not ({'full', 'ai_session', '_popup', '_to_field'} & set(q.keys())):
            from django.shortcuts import redirect
            from django.urls import reverse
            url = reverse('printing:quick_invoice')
            if str(q.get('customer', '')).isdigit():
                url += f"?customer={q['customer']}"
            return redirect(url)
        return super().add_view(request, form_url, extra_context)

    def get_fieldsets(self, request, obj=None):
        """📅 «تاريخ الفاتورة» بيتعدّل في الفاتورة الموجودة بس — الجديدة بتاخد النهارده
        (وللفاتورة القديمة الجديدة: الفاتورة السريعة فيها خانة تاريخ)."""
        fieldsets = super().get_fieldsets(request, obj)
        if obj is None or not obj.pk:
            return [(name, {**opts, 'fields': tuple(f for f in opts.get('fields', ()) if f not in self.ADD_HIDDEN)})
                    for name, opts in fieldsets]
        out = []
        for name, opts in fieldsets:
            fields = list(opts.get('fields', ()))
            if 'status' in fields and 'date_created' not in fields:
                fields.insert(fields.index('status') + 1, 'date_created')
                opts = {**opts, 'fields': tuple(fields)}
            out.append((name, opts))
        return out

    fieldsets = (
        (_('📋 بيانات الطلب'), {
            'fields': ('order_number', 'customer', 'branch', 'status', 'date_due', 'date_delivered', 'order_actions'),
        }),
        (_('💰 المالي'), {
            'fields': ('total_amount', 'discount', 'tax_rate', 'tax_amount', 'paid_amount'),
            'description': _('الإجمالي بيتحسب تلقائياً من أسعار المهام (لو فيه مهام مسعّرة). '
                             'المدفوع بيتحسب من الدفعات تحت — سجّل الدفعة في جدول «الدفعات على الطلب».'),
        }),
        (_('📁 الملفات'), {
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
    from django.conf import settings as _s
    # 🐛 [FIX]: رسالة الواتساب كان فيها رابط من غير دومين («/printing/proof/…») — العميل بيستلم
    #    رابط مايتفتحش. دلوقتي الرابط كامل على دومين المطبعة (زي رابط عرض السعر).
    path = f"/printing/proof/{proof.share_token}/"
    try:
        link = f"{'http' if _s.DEBUG else 'https'}://{connection.tenant.get_primary_domain().domain}{path}"
    except Exception:
        link = path
    msg = _urlquote(f"بروفة «{proof.job.description[:60]}» — راجعها ووافق أو اطلب تعديل: {link}")
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
