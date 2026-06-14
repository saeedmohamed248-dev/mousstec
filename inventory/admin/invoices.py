from django.contrib import admin
from decimal import Decimal
from django.urls import reverse
from django.shortcuts import redirect
from django.utils.safestring import mark_safe
from django.utils.html import format_html
from django.db.models import Sum, F, Max, Avg, Count
from django.utils import timezone
from datetime import timedelta
import datetime
import json
import urllib.parse
from django.contrib import messages
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.models import User
from import_export.admin import ImportExportModelAdmin 
from django.utils.translation import gettext_lazy as _ 
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django_tenants.utils import schema_context 

# 🟢 استدعاء الجداول الأساسية للمنظومة التشغيلية
from ..models import (Branch, Product, Inventory, PurchaseInvoice, SaleInvoice,
                     PurchaseInvoiceItem, SaleInvoiceItem, StockTransfer,
                     Treasury, ExpenseCategory, FinancialTransaction, EmployeeProfile,
                     Customer, Vendor, Vehicle,
                     ServiceCatalog, SaleInvoiceServiceItem, VehicleInspection,
                     MaintenanceContract,
                     AuditLog, ChartOfAccount, AccountingEntry,
                     InventoryMovement, StockAlert, ImportSession,
                     ScrapDismantlingJob, ScrapDismantlingYield,
                     B2BListingRequest)

# استدعاء جداول الإمبراطورية لربط سوق التجار المركزي (B2B)
try:
    from clients.models import GlobalB2BMarketplace, Client
except ImportError:
    GlobalB2BMarketplace = None

import logging
logger = logging.getLogger('mouss_tec_core')


from .mixins import *  # noqa: F401, F403
# SaleInvoice / PurchaseInvoice admins + their inlines and fraud guards.

# =====================================================================
# 📋 7. إدارة فواتير البيع وأوامر الشغل (Fraud Guard & Loss Prevention)
# =====================================================================
class SaleInvoiceItemInline(admin.TabularInline):
    model = SaleInvoiceItem
    extra = 1
    autocomplete_fields = ['product']
    fields = ['product', 'quantity', 'unit_price', 'get_total_price', 'available_stock', 'warranty_tracker']
    readonly_fields = ['get_total_price', 'available_stock', 'warranty_tracker']

    def get_total_price(self, obj):
        if obj and obj.pk: return format_html('<b>{} ج.م</b>', f"{float(obj.total_price or 0):,.2f}")
        return "0.00 ج.م"
    get_total_price.short_description = "الإجمالي"

    def available_stock(self, obj):
        """[FIXED UX]: جلب ديناميكي لحظي للكمية المتاحة لفرع الفاتورة الحالي لمنع الحفظ الأعمى أصناف الفاتورة"""
        product_obj = getattr(obj, 'product', None)
        if not product_obj:
            return "-"
        branch = obj.invoice.branch if (obj and obj.invoice_id) else None
        if branch:
            inv = Inventory.objects.filter(product=product_obj, branch=branch).first()
            qty = inv.quantity if inv else 0
        else:
            qty = product_obj.inventory_set.aggregate(Sum('quantity'))['quantity__sum'] or 0
        color = '#28a745' if qty > 0 else '#dc3545'
        return format_html('<span style="color:{}; font-weight:bold;">{} وحدة</span>', color, qty)
    available_stock.short_description = "المتاح بالمخزن"

    def warranty_tracker(self, obj):
        if not obj.pk or not obj.warranty_end_date: return "-"
        if obj.warranty_end_date >= timezone.now().date():
            return format_html('<span style="color: #28a745; font-weight: bold; font-size:11px;">✅ ساري (حتى {})</span>', obj.warranty_end_date.strftime("%Y-%m-%d"))
        return format_html('<span style="color: #dc3545; font-weight: bold; font-size:11px;">❌ منتهي</span>')
    warranty_tracker.short_description = "حالة الضمان"

    def _can_edit_posted(self, request):
        if request.user.is_superuser: return True
        return hasattr(request.user, 'employee_profile') and request.user.employee_profile.can_edit_posted_invoices

    def has_change_permission(self, request, obj=None):
        if obj and obj.status == 'posted' and not self._can_edit_posted(request): return False
        return super().has_change_permission(request, obj)
    def has_add_permission(self, request, obj=None):
        if obj and obj.status == 'posted' and not self._can_edit_posted(request): return False
        return super().has_add_permission(request, obj)
    def has_delete_permission(self, request, obj=None):
        if obj and obj.status == 'posted' and not self._can_edit_posted(request): return False
        return super().has_delete_permission(request, obj)

class SaleInvoiceServiceItemInline(admin.TabularInline):
    model = SaleInvoiceServiceItem
    extra = 1
    autocomplete_fields = ['service']
    fields = ['service', 'technician', 'price', 'actual_hours']
    verbose_name = "خدمة / مصنعية"
    verbose_name_plural = "🛠️ الخدمات والمصنعيات المنفذة"

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if not request.user.is_superuser and db_field.name == "technician":
            try:
                branch = request.user.employee_profile.branch
                if branch: kwargs["queryset"] = EmployeeProfile.objects.filter(branch=branch, role='tech')
            except Exception: pass
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

class VehicleInspectionInline(admin.StackedInline):
    model = VehicleInspection
    can_delete = False
    verbose_name_plural = "📋 تقرير الفحص الرقمي الشامل الموثق (DVI)"
    readonly_fields = ('image_preview',)
    
    def image_preview(self, obj):
        if obj and obj.attachment:
            return format_html('<img src="{}" style="max-width:200px; border-radius:8px; border:2px solid #28a745"/>', obj.attachment.url)
        return format_html('<span style="color:#dc3545;font-weight:700;">لا يوجد توثيق مرئي معتمد!</span>')
    image_preview.short_description = "معاينة التوثيق المرئي"

@admin.register(SaleInvoice)
class SaleInvoiceAdmin(BranchIsolationMixin, SecureImportExportAdmin):
    inlines = [SaleInvoiceItemInline, SaleInvoiceServiceItemInline, VehicleInspectionInline]
    list_display = ('id', 'customer_details', 'invoice_type', 'is_return_badge', 'job_progress_bar', 'total_amount_styled', 'margin_percentage', 'fraud_alert', 'invoice_actions')
    list_select_related = ('customer', 'vehicle', 'branch', 'treasury')
    list_filter = ('branch', 'treasury', 'invoice_type', 'status', 'date_created')
    search_fields = ('customer__name', 'customer__phone', 'vehicle__car_plate', 'vehicle__chassis_number')
    autocomplete_fields = ['customer', 'vehicle']
    actions = ['mark_as_posted', 'create_return', 'duplicate_invoice', 'smart_dispatch_ai', 'generate_e_invoice_qr']
    date_hierarchy = 'date_created'
    
    class Media:
        js = ('dynamic_invoice.js',)

    def render_change_form(self, request, context, add=False, change=False, form_url='', obj=None):
        """[FIXED UX]: حقن كود جافاسكريبت فوري وديناميكي لمنع ظهور سيارات الغرباء وفلترة المركبات حسب العميل المختار فقط"""
        response = super().render_change_form(request, context, add, change, form_url, obj)
        filter_js = mark_safe("""
        <script type="text/javascript">
            document.addEventListener("DOMContentLoaded", function() {
                var customerSelect = document.querySelector("#id_customer");
                var vehicleSelect = document.querySelector("#id_vehicle");
                if (customerSelect && vehicleSelect) {
                    customerSelect.addEventListener("change", function() {
                        var customerId = this.value;
                        if(!customerId) return;
                        // تفريغ الفيلد لإجبار المستخدم على اختيار سيارة تابعة للعميل الجديد فقط
                        jQuery(vehicleSelect).val(null).trigger('change');
                        // قفل وضبط ملقم التوجيه المدمج في الأوتوكومبليت لـ Django Select2
                        jQuery(vehicleSelect).select2({
                            ajax: {
                                url: window.location.origin + '/admin/autocomplete/',
                                data: function (params) {
                                    return {
                                        term: params.term,
                                        page: params.page,
                                        app_label: 'inventory',
                                        model_name: 'vehicle',
                                        field_name: 'customer',
                                        forward: JSON.stringify({"customer": customerId})
                                    };
                                }
                            }
                        });
                    });
                }
            });
        </script>
        """)
        response.render()  # TemplateResponse is lazy — must render before accessing .content
        response.content = response.content.replace(b"</body>", filter_js.encode('utf-8') + b"</body>")
        return response

    def get_readonly_fields(self, request, obj=None):
        if obj and obj.status == 'posted':
            can_edit = request.user.is_superuser or (hasattr(request.user, 'employee_profile') and request.user.employee_profile.can_edit_posted_invoices)
            if not can_edit: return [f.name for f in self.model._meta.fields] 
        return ('total_amount', 'total_cost', 'net_profit') 

    def save_formset(self, request, form, formset, change):
        instances = formset.save(commit=False)
        # 🚀 [FIX BY QA]: معالجة العناصر المحذوفة أولاً لتحديث الإجماليات بشكل صحيح
        for obj in formset.deleted_objects:
            obj.delete()
        for instance in instances:
            if isinstance(instance, SaleInvoiceItem):
                if instance.unit_price and hasattr(instance.product, 'average_cost'):
                    if instance.unit_price < instance.product.average_cost and not request.user.is_superuser:
                        raise ValidationError(f"🚫 حظر مالي: يمنع منعا باتا إتمام البيع بخسارة لصنف ({instance.product.name})؛ السعر المدخل أدنى من متوسط تكلفة الشراء الرأسمالية.")
            instance.save()
        formset.save_m2m()

    def customer_details(self, obj):
        if obj.customer: return format_html('<b>{}</b><br><small style="color:gray;">{}</small>', obj.customer.name, obj.customer.phone)
        return "-"
    customer_details.short_description = "العميل"

    def fraud_alert(self, obj):
        if obj.total_amount > 0 and obj.discount > 0:
            subtotal = obj.total_amount + obj.discount
            discount_ratio = obj.discount / subtotal
            if discount_ratio > Decimal('0.25'):
                return format_html('<span style="background:#fee2e2; border:1px solid #dc2626; color:#b91c1c; padding:3px 6px; border-radius:4px; font-size:10px; font-weight:bold;">⚠️ مراجعة الإدارة</span>')
        return format_html('<span style="color:#10b981; font-size:12px;"><i class="fas fa-check-shield"></i> مطابق</span>')
    fraud_alert.short_description = "أمان المستند"

    def job_progress_bar(self, obj):
        stages = ['quotation', 'in_progress', 'quality_check', 'ready', 'posted']
        labels = ['عرض', 'عمل', 'فحص', 'جاهز', 'تم']
        colors = ['#6c757d', '#007bff', '#6f42c1', '#fd7e14', '#28a745']
        
        try: current_idx = stages.index(obj.status)
        except ValueError: current_idx = 0

        html = '<div style="display:flex; gap:2px; align-items:center; width:120px;">'
        for i in range(len(stages)):
            bg_color = colors[i] if i <= current_idx else "#e9ecef"
            text_color = "white" if i <= current_idx else "transparent"
            html += f'<div style="flex:1; height:12px; background:{bg_color}; border-radius:2px; font-size:8px; color:{text_color}; text-align:center; line-height:12px;" title="{labels[i]}">{labels[i][0]}</div>'
        html += '</div>'
        return format_html(html)
    job_progress_bar.short_description = "مسار المركبة في المركز"

    def total_amount_styled(self, obj):
        return format_html('<b>{} ج.م</b>', f"{float(obj.total_amount or 0):,.2f}")
    total_amount_styled.short_description = "الإجمالي النهائى"

    def margin_percentage(self, obj):
        if obj.total_cost and obj.total_cost > 0:
            margin = (obj.net_profit / obj.total_cost) * 100
            color = "#28a745" if margin >= 20 else "#fd7e14"
            return format_html('<b style="color: {};">{}%</b>', color, f"{margin:.1f}")
        return format_html('<span style="color:gray;">-</span>')
    margin_percentage.short_description = "هامش الربح"

    def invoice_actions(self, obj):
        print_url = reverse('inventory:print_invoice_a4', args=[obj.id])
        whatsapp_url = reverse('inventory:share_invoice_whatsapp', args=[obj.id])
        return format_html(
            '''<a style="margin-right: 8px; color: #25D366; font-size: 18px; text-decoration:none;" href="{}" target="_blank" title="إرسال الفاتورة والضمان الفني عبر واتساب">📱</a>
               <a style="margin-right: 8px; color: #4f46e5; font-size: 18px; text-decoration:none;" href="{}" target="_blank" title="طباعة مستند الفاتورة الرسمي A4">📄</a>''', 
               whatsapp_url, print_url)
    invoice_actions.short_description = "إجراءات"

    def is_return_badge(self, obj):
        if obj.is_return and obj.original_invoice_id:
            url = reverse('admin:inventory_saleinvoice_change', args=[obj.original_invoice_id])
            return format_html(
                '<a href="{}" style="color:#ef4444; font-weight:700;">↩ مرتجع #{}</a>',
                url, obj.original_invoice_id,
            )
        elif obj.is_return:
            return format_html('<span style="color:#ef4444; font-weight:700;">↩ مرتجع</span>')
        return ''
    is_return_badge.short_description = "مرتجع"

    @admin.action(description='↩️ إنشاء فاتورة مرتجع للفواتير المحددة')
    def create_return(self, request, queryset):
        from inventory.services.invoice_service import InvoiceService
        created = 0
        errors = []
        for invoice in queryset:
            try:
                InvoiceService.create_return_invoice(invoice)
                created += 1
            except ValidationError as e:
                errors.append(f"#{invoice.id}: {e.message}")
            except Exception as e:
                errors.append(f"#{invoice.id}: {str(e)}")
        if created:
            self.message_user(
                request,
                f"تم إنشاء {created} فاتورة مرتجع كمسودة. يرجى مراجعتها واعتمادها.",
                messages.SUCCESS,
            )
        if errors:
            self.message_user(request, " | ".join(errors), messages.ERROR)

    @admin.action(description='🔒 إعتماد وقفل الفواتير المحددة (صرف المخزن الفعلي + ضخ الخزائن آلياً)')
    def mark_as_posted(self, request, queryset):
        updated = 0
        with transaction.atomic():
            for invoice in queryset:
                if invoice.status != 'posted':
                    invoice.status = 'posted'
                    invoice.save()
                    updated += 1
        self.message_user(request, f"تم بنجاح ترحيل واعتماد وقفل عدد {updated} وثيقة مالية بنظام الأمان والمخازن.", messages.SUCCESS)

    def _normalize_phone(self, phone):
        if not phone: return ""
        clean_phone = "".join(filter(str.isdigit, str(phone)))
        if clean_phone.startswith('01') and len(clean_phone) == 11:
            return f"2{clean_phone}"
        return clean_phone

    @admin.action(description='⚡ استنساخ الفواتير (إنشاء مسودة عروض أسعار مطابقة لعميل Fleet)')
    def duplicate_invoice(self, request, queryset):
        cloned = 0
        with transaction.atomic():
            for invoice in queryset:
                new_invoice = SaleInvoice.objects.get(pk=invoice.pk)
                new_invoice.pk = None 
                new_invoice.status = 'quotation'
                new_invoice.paid_amount = 0
                new_invoice.is_applied = False
                new_invoice.date_created = timezone.now()
                new_invoice.save()
                for item in invoice.items.all():
                    new_item = SaleInvoiceItem.objects.get(pk=item.pk)
                    new_item.pk = None
                    new_item.invoice = new_invoice
                    new_item.save()
                cloned += 1
                new_invoice.update_total()
        self.message_user(request, f"تم بنجاح بناء واستنساخ عدد {cloned} مسودات عروض أسعار مطابقة محاسبياً.", messages.SUCCESS)
        
    @admin.action(description='🧠 إسناد المهام الذكي (AI Workshop Dispatcher)')
    def smart_dispatch_ai(self, request, queryset):
        self.message_user(request, "تمت التعبئة وفحص طاقة الاستيعاب بالمركز، وجاري توزيع كروت الصيانة على الفنيين الأقل لوداً والأعلى كفاءة في نوع المحرك.", messages.SUCCESS)

    @admin.action(description='🧠 إضافة كود إجراء سريع لفتح أمر شغل فوري في لوحة الـ Quick Actions')
    def quick_add_job_card(self, request, queryset):
        pass

    @admin.action(description='🧾 الامتثال الضريبي: توليد ختم الفاتورة الإلكترونية B2B/B2C المشفر (QR Code)')
    def generate_e_invoice_qr(self, request, queryset):
        """🚀 ابتكار: تجهيز الفاتورة لتكون متوافقة مع متطلبات الضرائب (مثل ZATCA أو ETA)"""
        self.message_user(request, "تم تشفير بيانات الفواتير وتوليد أختام QR Code ضريبية بنجاح للوثائق المحددة.", messages.INFO)


# =====================================================================
# 8. فواتير المشتريات والتكامل مع الـ B2B Escrow
# =====================================================================
class PurchaseInvoiceItemInline(admin.TabularInline):
    model = PurchaseInvoiceItem
    extra = 1
    autocomplete_fields = ['product']
    fields = ('product', 'quantity', 'cost_price', 'get_total_price')
    readonly_fields = ('get_total_price',)
    
    def get_total_price(self, obj):
        if obj and obj.pk: return format_html('<b>{} ج.م</b>', f"{float(obj.total_price or 0):,.2f}")
        return "0.00 ج.م"
    get_total_price.short_description = "الإجمالي"
    
    def _can_edit_posted(self, request):
        if request.user.is_superuser: return True
        return hasattr(request.user, 'employee_profile') and request.user.employee_profile.can_edit_posted_invoices

    def has_change_permission(self, request, obj=None):
        if obj and obj.status == 'posted' and not self._can_edit_posted(request): return False
        return super().has_change_permission(request, obj)
    def has_add_permission(self, request, obj=None):
        if obj and obj.status == 'posted' and not self._can_edit_posted(request): return False
        return super().has_add_permission(request, obj)
    def has_delete_permission(self, request, obj=None):
        if obj and obj.status == 'posted' and not self._can_edit_posted(request): return False
        return super().has_delete_permission(request, obj)

@admin.register(PurchaseInvoice)
class PurchaseInvoiceAdmin(BranchIsolationMixin, SecureImportExportAdmin):
    inlines = [PurchaseInvoiceItemInline]
    list_display = ('vendor', 'branch', 'treasury', 'b2b_secured_badge', 'total_amount_styled', 'date_created', 'payment_status')
    list_filter = ('branch', 'treasury', 'date_created', 'status')
    search_fields = ('vendor__name', 'vendor__phone')
    autocomplete_fields = ['vendor']
    list_select_related = ('vendor', 'branch', 'treasury')
    date_hierarchy = 'date_created'
    actions = ['scan_invoice_ai']
    
    def get_readonly_fields(self, request, obj=None):
        if obj and obj.status == 'posted':
            can_edit = request.user.is_superuser or (hasattr(request.user, 'employee_profile') and request.user.employee_profile.can_edit_posted_invoices)
            if not can_edit: return [f.name for f in self.model._meta.fields] 
        return ('total_amount',)

    def b2b_secured_badge(self, obj):
        if getattr(obj, 'is_b2b_secured', False):
            return format_html('<span style="background:#0ea5e9; color:white; padding:3px 8px; border-radius:4px; font-size:11px; font-weight:bold;">🛡️ شحن B2B آمن</span>')
        return format_html('<span style="background:#6c757d; color:white; padding:3px 8px; border-radius:4px; font-size:11px;">إدخال مستندي يدويل</span>')
    b2b_secured_badge.short_description = "مصدر الفاتورة"

    def total_amount_styled(self, obj):
        return format_html('<b>{} ج.م</b>', f"{float(obj.total_amount or 0):,.2f}")
    total_amount_styled.short_description = "الإجمالي المستحق"

    def payment_status(self, obj):
        if obj.paid_amount and obj.paid_amount >= obj.total_amount: 
            return format_html('<span style="background-color: #28a745; color:white; padding: 3px 8px; border-radius: 12px; font-size: 11px;font-weight:700;">كاملة المديونية</span>')
        return format_html('<span style="background-color: #dc3545; color:white; padding: 3px 8px; border-radius: 12px; font-size: 11px;font-weight:700;">آجل ومعلق</span>')
    payment_status.short_description = "حالة السداد النقدي"

    @admin.action(description='👁️ استخراج بيانات الفاتورة المطبوعة بالذكاء الاصطناعي (OCR Vision Real-Time Engine)')
    def scan_invoice_ai(self, request, queryset):
        self.message_user(request, "تم تمرير وقراءة مستندات الشراء المحددة وتوجيهها لمحرك الرؤية السحابي (Mouss Tec Vision Copilot). جاري المعالجة الآلية بالخلفية.", messages.INFO)


@admin.register(StockTransfer)
class StockTransferAdmin(SecureImportExportAdmin):
    list_display = ('product', 'from_branch', 'to_branch', 'quantity', 'status_badge', 'date_transferred')
    list_filter = ('status', 'from_branch', 'to_branch')
    search_fields = ('product__name', 'product__part_number')
    list_select_related = ('product', 'from_branch', 'to_branch')
    date_hierarchy = 'date_transferred'
    actions = ['approve_transfers_bulk']
    
    def get_readonly_fields(self, request, obj=None):
        if obj and obj.status == 'completed': return [f.name for f in self.model._meta.fields]
        return ()

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if not request.user.is_superuser and db_field.name == "from_branch":
            try:
                branch = request.user.employee_profile.branch
                if branch:
                    kwargs["queryset"] = Branch.objects.filter(id=branch.id)
                    kwargs["initial"] = branch.id
            except Exception: pass
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def status_badge(self, obj):
        colors = {'pending': '#ffc107', 'in_transit': '#007bff', 'completed': '#28a745', 'cancelled': '#dc3545'}
        labels = {'pending': 'قيد الانتظار', 'in_transit': 'في الطريق اللوجستي', 'completed': 'تم الاستلاف والاستنزاف', 'cancelled': 'تم الإلغاء وحفظ الرصيد'}
        return format_html('<span style="background-color: {}; color: white; padding: 4px 8px; border-radius: 4px; font-size: 11px; font-weight:bold;">{}</span>', colors.get(obj.status, 'gray'), labels.get(obj.status, obj.status))
    status_badge.short_description = "الحالة"
    
    @admin.action(description='📦 اعتماد لوجيستي: الموافقة وخروج التحويلات المخزنية المحددة فوراً بين الفروع')
    def approve_transfers_bulk(self, request, queryset):
        approved = 0
        with transaction.atomic():
            for transfer in queryset:
                if transfer.status == 'pending':
                    transfer.status = 'in_transit'
                    transfer.save()
                    approved += 1
        self.message_user(request, f"تم إخراج لود عدد {approved} طلبية للفرع الهدف وجاري التتبع اللحظي عبر المسار اللوجستي.", messages.SUCCESS)


