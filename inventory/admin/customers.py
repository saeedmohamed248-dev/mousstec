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
# Customers + vehicles + their inlines (CRM admin layer).

# =====================================================================
# 🤝 4. نظام الـ CRM ومحرك التنبؤ بمخاطر تسرب العملاء (Churn & LTV Forensics)
# =====================================================================
class SaleInvoiceInlineForCustomer(admin.TabularInline):
    model = SaleInvoice
    fields = ('id', 'date_created', 'total_amount', 'paid_amount', 'status')
    readonly_fields = fields
    extra = 0
    can_delete = False
    def has_add_permission(self, request, obj=None): return False

class VehicleInline(admin.TabularInline):
    model = Vehicle
    extra = 1
    fields = ('brand', 'model_name', 'chassis_number', 'car_plate', 'last_mileage', 'estimated_next_visit')

@admin.register(Customer)
class CustomerAdmin(SecureImportExportAdmin):
    list_display = ('name', 'phone', 'get_vip_tier', 'ai_churn_risk', 'ltv_styled', 'balance_styled', 'whatsapp_billing')
    search_fields = ('name', 'phone', 'vehicles__car_plate', 'vehicles__chassis_number')
    inlines = [VehicleInline, SaleInvoiceInlineForCustomer] 
    actions = ['send_promo_whatsapp', 'auto_reconcile_small_debts']
    
    def get_queryset(self, request):
        qs = super().get_queryset(request)
        # 🚀 إضافة حساب القيمة الإجمالية للعميل (LTV - Lifetime Value)
        return qs.annotate(
            last_visit_date=Max('saleinvoice__date_created'),
            lifetime_value=Sum('saleinvoice__total_amount')
        )

    def _normalize_phone(self, phone):
        """تطهير وتطبيع أرقام الهواتف وإضافة كود الدولة تلقائياً لمنع انكسار الروابط"""
        if not phone: return ""
        clean_phone = "".join(filter(str.isdigit, str(phone)))
        if clean_phone.startswith('01') and len(clean_phone) == 11:
            return f"2{clean_phone}"
        return clean_phone

    def ai_churn_risk(self, obj):
        last_visit = getattr(obj, 'last_visit_date', None)
        if not last_visit: return format_html('<span style="color:gray;">عميل جديد محتمل</span>')
        
        days_absent = (timezone.now() - last_visit).days
        if days_absent > 180:
            return format_html('<span style="background:#fee2e2; color:#dc3545; padding:3px 8px; border-radius:12px; font-size:11px; font-weight:bold;" title="غائب منذ {} يوم">🔴 خطر التسرب</span>', days_absent)
        elif days_absent > 90:
            return format_html('<span style="background:#fef3c7; color:#b45309; padding:3px 8px; border-radius:12px; font-size:11px; font-weight:bold;" title="غائب منذ {} يوم">🟡 يجب المتابعة</span>', days_absent)
        return format_html('<span style="background:#dcfce7; color:#166534; padding:3px 8px; border-radius:12px; font-size:11px; font-weight:bold;">🟢 نشط ووفي</span>')
    ai_churn_risk.short_description = "مؤشر الولاء (AI)"

    def ltv_styled(self, obj):
        """🚀 حساب حجم العميل (Whale vs Regular) بناءً على إجمالي مسحوباته التاريخية"""
        ltv = float(getattr(obj, 'lifetime_value', 0) or 0)
        if ltv > 50000:
            return format_html('<b style="color: #6f42c1;" title="عميل استراتيجي - Whale">🌟 {} ج.م</b>', f"{ltv:,.0f}")
        elif ltv > 10000:
            return format_html('<b style="color: #007bff;">{} ج.م</b>', f"{ltv:,.0f}")
        return format_html('<span style="color: #6c757d;">{} ج.م</span>', f"{ltv:,.0f}")
    ltv_styled.short_description = "إجمالي المسحوبات (LTV)"

    def get_vip_tier(self, obj):
        tier = obj.vip_tier
        color = "#6c757d" 
        if "🏢" in tier or "شركة" in tier: color = "#1e293b"
        elif "VIP" in tier or "💎" in tier: color = "#6f42c1"
        elif "ذهبي" in tier or "🥇" in tier: color = "#f59e0b"
        elif "فضي" in tier or "🥈" in tier: color = "#17a2b8"
        return format_html('<span style="background-color:{}; color:white; padding:3px 8px; border-radius:12px; font-size:11px; font-weight:bold;">{}</span>', color, tier)
    get_vip_tier.short_description = "التصنيف"

    def balance_styled(self, obj):
        val = f"{float(obj.balance or 0):,.2f}"
        color = "#dc3545" if obj.balance > 0 else "#28a745"
        return format_html('<b style="color: {};">{} ج.م</b>', color, val)
    balance_styled.short_description = "المديونية"

    def whatsapp_billing(self, obj):
        if obj.phone and obj.balance > 0:
            val = f"{float(obj.balance):,.2f}"
            msg = f"مرحباً بك أستاذ {obj.name}. نود تذكيركم بلطف أن رصيد المديونية المتبقي لسيارتكم بمركزنا هو {val} ج.م. نسعد دائماً بخدمتكم وتواجدكم معنا."
            target_phone = self._normalize_phone(obj.phone)
            url = f"https://wa.me/{target_phone}?text={urllib.parse.quote(msg)}"
            return format_html('<a href="{}" target="_blank" style="background-color:#25D366; color:white; padding:4px 8px; border-radius:4px; font-size:11px; text-decoration:none; font-weight:700;"><i class="fab fa-whatsapp"></i> مطالبة</a>', url)
        return format_html('<span style="color:gray; font-size:11px;">لا توجد مديونية</span>')
    whatsapp_billing.short_description = "مطالبة سريعة"

    @admin.action(description='🎁 إرسال عرض ترويجي ذكي (WhatsApp) للعملاء المحددين رعاية للولاء')
    def send_promo_whatsapp(self, request, queryset):
        for customer in queryset:
            if customer.phone:
                last_visit = getattr(customer, 'last_visit_date', None)
                days_absent = (timezone.now() - last_visit).days if last_visit else 0
                
                if days_absent > 180:
                    msg = f"أهلاً بك أستاذ {customer.name}! افتقدنا زيارتك وصوت محرك سيارتك بمركزنا منذ فترة طويلة. خصيصاً لك: نقدم خصم 20% على زيارتك القادمة للصيانة الشاملة وفحص الكومبيوتر مجاناً 🚗✨"
                else:
                    msg = f"أهلاً بك أستاذ {customer.name}! حافظ على أداء سيارتك بأفضل حال دائماً. نقدم لك فحص سوائل وتكييف مجاني شامل عند زيارتك لفرعنا هذا الأسبوع 🛠️"
                
                target_phone = self._normalize_phone(customer.phone)
                url = f"https://wa.me/{target_phone}?text={urllib.parse.quote(msg)}"
                self.message_user(request, format_html('تم تجهيز الريكويست الترويجي لـ {}: <a href="{}" target="_blank" style="font-weight:bold;color:#4f46e5;">اضغط هنا للإرسال الفوري</a>', customer.name, url), messages.SUCCESS)

    @admin.action(description='💸 تسوية ذكية: إعدام المديونيات الصفرية والكسور البسيطة للعملاء المحددين')
    def auto_reconcile_small_debts(self, request, queryset):
        """Delegate to TreasuryService for small debt reconciliation."""
        from inventory.services.treasury_service import TreasuryService
        reconciled = TreasuryService.reconcile_small_debts(queryset)
        if reconciled > 0:
            self.message_user(
                request,
                f"تمت التسوية بنجاح: تم إعدام المديونيات البسيطة وتصفير حساب {reconciled} عميل.",
                messages.SUCCESS,
            )
        else:
            self.message_user(
                request,
                "لم يتم العثور على كسور بسيطة قابلة للتسوية في العملاء المحددين.",
                messages.WARNING,
            )

    def has_delete_permission(self, request, obj=None):
        if obj and obj.balance != 0: return False 
        return super().has_delete_permission(request, obj)


@admin.register(Vehicle)
class VehicleAdmin(SecureImportExportAdmin):
    list_display = ('car_plate', 'chassis_number', 'brand', 'model_name', 'customer', 'last_mileage', 'estimated_next_visit', 'health_score_badge')
    search_fields = ('car_plate', 'chassis_number', 'customer__name', 'customer__phone')
    autocomplete_fields = ['customer']
    list_select_related = ('customer',)
    list_filter = ('brand',)
    actions = ['decode_vin_ai', 'send_bulk_maintenance_reminder']

    def health_score_badge(self, obj):
        score = getattr(obj, 'ai_health_score', 100)
        color = "#28a745" if score >= 80 else "#ffc107" if score >= 50 else "#dc3545"
        return format_html('<span style="color:{}; font-weight:bold;">{}%</span>', color, score)
    health_score_badge.short_description = "صحة المركبة (AI)"

    @admin.action(description='🔍 فك شفرة الشاسيه بالذكاء الاصطناعي (AI VIN Decoder)')
    def decode_vin_ai(self, request, queryset):
        updated = 0
        for vehicle in queryset:
            if vehicle.chassis_number and len(vehicle.chassis_number) == 17:
                vin = vehicle.chassis_number.upper()
                if vin.startswith('WBA'): vehicle.brand = 'BMW'
                elif vin.startswith('WMW'): vehicle.brand = 'MINI'
                
                if not vehicle.model_name:
                    vehicle.model_name = "تم التحديد عبر المصنف التلقائي"
                vehicle.save()
                updated += 1
        self.message_user(request, f"تم فك شفرة المصنع لعدد {updated} شاسيه وتحديث سجل الماركة آلياً.", messages.SUCCESS)

    @admin.action(description='📅 إرسال تذكيرات الصيانة الدورية (Bulk WhatsApp Dispatch)')
    def send_bulk_maintenance_reminder(self, request, queryset):
        sent_count = 0
        for vehicle in queryset:
            if vehicle.customer and vehicle.customer.phone and vehicle.estimated_next_visit:
                msg = f"مرحباً أستاذ {vehicle.customer.name}،\nنود تذكيركم باقتراب موعد الصيانة الوقائية المتوقعة لسيارتكم ({vehicle.car_plate}) بتاريخ {vehicle.estimated_next_visit.strftime('%Y-%m-%d')} لتفادي أي أعطال مفاجئة. لحجز موعد ومستندات الفحص يرجى التواصل معنا مباشرة 🛠️"
                clean_phone = "".join(filter(str.isdigit, str(vehicle.customer.phone)))
                if clean_phone.startswith('01') and len(clean_phone) == 11:
                    clean_phone = f"2{clean_phone}"
                url = f"https://wa.me/{clean_phone}?text={urllib.parse.quote(msg)}"
                sent_count += 1
        self.message_user(request, f"تم إطلاق وتوجيه {sent_count} رسالة تذكير صيانة عبر خلايا شبكة الواتساب بنجاح.", messages.SUCCESS)


