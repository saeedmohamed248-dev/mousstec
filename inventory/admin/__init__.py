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


# Base mixins live in their own submodule and are re-exported here so the
# rest of this file (and any external imports) see them unchanged.
from .mixins import *  # noqa: F401, F403

from .organization import *  # noqa: F401, F403
from .customers import *  # noqa: F401, F403
from .catalog import *  # noqa: F401, F403

from .invoices import *  # noqa: F401, F403
from .finance import *  # noqa: F401, F403
from .dashboard import *  # noqa: F401, F403

# =====================================================================
# 📋 11. سجل المراجعة والتدقيق (Audit Trail — Read-Only)
# =====================================================================
@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ('timestamp', 'user', 'action_badge', 'model_name', 'object_id', 'object_repr_short', 'ip_address')
    list_filter = ('action', 'model_name', 'timestamp')
    search_fields = ('object_repr', 'object_id', 'user__username', 'ip_address')
    date_hierarchy = 'timestamp'
    readonly_fields = ('timestamp', 'user', 'action', 'model_name', 'object_id', 'object_repr', 'changes_json', 'ip_address')

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_module_permission(self, request):
        if connection.schema_name == 'public':
            return False
        return super().has_module_permission(request)

    def action_badge(self, obj):
        colors = {'create': '#28a745', 'update': '#007bff', 'delete': '#dc3545'}
        icons = {'create': '➕', 'update': '✏️', 'delete': '🗑️'}
        return format_html(
            '<span style="background:{}; color:white; padding:3px 8px; border-radius:4px; font-size:11px; font-weight:bold;">{} {}</span>',
            colors.get(obj.action, 'gray'), icons.get(obj.action, ''), obj.get_action_display()
        )
    action_badge.short_description = "العملية"

    def object_repr_short(self, obj):
        text = obj.object_repr or ''
        return text[:60] + '...' if len(text) > 60 else text
    object_repr_short.short_description = "الوصف"


# =====================================================================
# 📊 12. دليل الحسابات المحاسبية (Chart of Accounts)
# =====================================================================
@admin.register(ChartOfAccount)
class ChartOfAccountAdmin(admin.ModelAdmin):
    list_display = ('code', 'name', 'account_type_badge', 'parent', 'balance_display', 'is_active')
    list_filter = ('account_type', 'is_active')
    search_fields = ('code', 'name')
    list_editable = ('is_active',)
    ordering = ('code',)

    def has_module_permission(self, request):
        if connection.schema_name == 'public':
            return False
        return super().has_module_permission(request)

    def account_type_badge(self, obj):
        colors = {
            'asset': '#007bff', 'liability': '#dc3545', 'equity': '#6f42c1',
            'revenue': '#28a745', 'expense': '#fd7e14',
        }
        return format_html(
            '<span style="background:{}; color:white; padding:3px 8px; border-radius:4px; font-size:11px;">{}</span>',
            colors.get(obj.account_type, 'gray'), obj.get_account_type_display()
        )
    account_type_badge.short_description = "نوع الحساب"

    def balance_display(self, obj):
        bal = obj.balance
        color = "#28a745" if bal >= 0 else "#dc3545"
        return format_html('<b style="color:{};">{} ج.م</b>', color, f"{float(bal):,.2f}")
    balance_display.short_description = "الرصيد"


# =====================================================================
# 📒 13. دفتر القيود المحاسبية (Accounting Ledger)
# =====================================================================
@admin.register(AccountingEntry)
class AccountingEntryAdmin(admin.ModelAdmin):
    list_display = ('entry_date', 'reference', 'account', 'description_short', 'debit_display', 'credit_display', 'linked_doc')
    list_filter = ('account__account_type', 'entry_date')
    search_fields = ('reference', 'description', 'account__name')
    date_hierarchy = 'entry_date'
    autocomplete_fields = ['account']
    readonly_fields = ('entry_date', 'reference', 'description', 'account', 'debit', 'credit',
                       'sale_invoice', 'purchase_invoice', 'financial_transaction', 'created_by')

    def has_module_permission(self, request):
        if connection.schema_name == 'public':
            return False
        return super().has_module_permission(request)

    def description_short(self, obj):
        text = obj.description or ''
        return text[:50] + '...' if len(text) > 50 else text
    description_short.short_description = "البيان"

    def debit_display(self, obj):
        if obj.debit > 0:
            return format_html('<b style="color:#dc3545;">{}</b>', f"{float(obj.debit):,.2f}")
        return '-'
    debit_display.short_description = "مدين"

    def credit_display(self, obj):
        if obj.credit > 0:
            return format_html('<b style="color:#28a745;">{}</b>', f"{float(obj.credit):,.2f}")
        return '-'
    credit_display.short_description = "دائن"

    def linked_doc(self, obj):
        if obj.sale_invoice:
            url = reverse('admin:inventory_saleinvoice_change', args=[obj.sale_invoice.pk])
            return format_html('<a href="{}">فاتورة بيع #{}</a>', url, obj.sale_invoice.pk)
        if obj.purchase_invoice:
            url = reverse('admin:inventory_purchaseinvoice_change', args=[obj.purchase_invoice.pk])
            return format_html('<a href="{}">فاتورة شراء #{}</a>', url, obj.purchase_invoice.pk)
        if obj.financial_transaction:
            url = reverse('admin:inventory_financialtransaction_change', args=[obj.financial_transaction.pk])
            return format_html('<a href="{}">حركة مالية #{}</a>', url, obj.financial_transaction.pk)
        return '-'
    linked_doc.short_description = "المستند"


# =====================================================================
# 📦 14. سجل حركات المخزون (Inventory Movements)
# =====================================================================
@admin.register(InventoryMovement)
class InventoryMovementAdmin(admin.ModelAdmin):
    list_display = ('created_at', 'product', 'branch', 'reason_badge', 'qty_change_display', 'quantity_before', 'quantity_after', 'note')
    list_filter = ('reason', 'branch', 'created_at')
    search_fields = ('product__name', 'product__part_number', 'note')
    date_hierarchy = 'created_at'
    readonly_fields = ('product', 'branch', 'reason', 'quantity_change', 'quantity_before',
                       'quantity_after', 'reference_type', 'reference_id', 'note', 'created_at', 'created_by')

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_module_permission(self, request):
        if connection.schema_name == 'public':
            return False
        return super().has_module_permission(request)

    def reason_badge(self, obj):
        colors = {
            'sale': '#dc3545', 'sale_return': '#28a745', 'purchase': '#007bff',
            'purchase_return': '#fd7e14', 'transfer_out': '#6c757d', 'transfer_in': '#17a2b8',
            'adjustment': '#6f42c1', 'scrap': '#343a40', 'manual': '#ffc107',
        }
        return format_html(
            '<span style="background:{}; color:white; padding:3px 8px; border-radius:4px; font-size:11px;">{}</span>',
            colors.get(obj.reason, 'gray'), obj.get_reason_display()
        )
    reason_badge.short_description = "السبب"

    def qty_change_display(self, obj):
        color = "#28a745" if obj.quantity_change > 0 else "#dc3545"
        return format_html('<b style="color:{}; font-size:13px;">{}</b>', color, f"{obj.quantity_change:+d}")
    qty_change_display.short_description = "التغيير"


# =====================================================================
# 🚨 15. تنبيهات نقص المخزون (Stock Alerts)
# =====================================================================
@admin.register(StockAlert)
class StockAlertAdmin(admin.ModelAdmin):
    list_display = ('created_at', 'product', 'branch', 'alert_badge', 'current_quantity', 'min_stock_level', 'status_badge')
    list_filter = ('alert_type', 'is_resolved', 'branch')
    search_fields = ('product__name', 'product__part_number')
    actions = ['mark_resolved']

    def has_module_permission(self, request):
        if connection.schema_name == 'public':
            return False
        return super().has_module_permission(request)

    def alert_badge(self, obj):
        if obj.alert_type == 'out_of_stock':
            return format_html('<span style="background:#dc3545; color:white; padding:3px 8px; border-radius:4px; font-size:11px; font-weight:bold;">⚠️ نفاد تام</span>')
        return format_html('<span style="background:#ffc107; color:#000; padding:3px 8px; border-radius:4px; font-size:11px; font-weight:bold;">📉 منخفض</span>')
    alert_badge.short_description = "نوع التنبيه"

    def status_badge(self, obj):
        if obj.is_resolved:
            return format_html('<span style="color:#28a745; font-weight:bold;">✅ تم الحل</span>')
        return format_html('<span style="color:#dc3545; font-weight:bold;">🔴 نشط</span>')
    status_badge.short_description = "الحالة"

    @admin.action(description='✅ وضع علامة "تم الحل" على التنبيهات المحددة')
    def mark_resolved(self, request, queryset):
        updated = queryset.filter(is_resolved=False).update(is_resolved=True, resolved_at=timezone.now())
        self.message_user(request, f"تم إغلاق {updated} تنبيه بنجاح.", messages.SUCCESS)


# =====================================================================
# 🔑 16. جلسات الاستيراد الآمن (Import Sessions)
# =====================================================================
@admin.register(ImportSession)
class ImportSessionAdmin(admin.ModelAdmin):
    list_display = ('session_id_short', 'entity_type', 'status_badge', 'total_rows', 'valid_rows', 'error_rows', 'conflict_rows', 'created_by', 'created_at')
    list_filter = ('entity_type', 'status')
    readonly_fields = ('session_id', 'entity_type', 'status', 'uploaded_file', 'original_filename',
                       'total_rows', 'valid_rows', 'error_rows', 'conflict_rows',
                       'validation_report', 'conflict_report', 'imported_ids', 'backup_snapshot',
                       'created_by', 'created_at', 'completed_at')

    def has_add_permission(self, request):
        return False

    def has_module_permission(self, request):
        if connection.schema_name == 'public':
            return False
        return super().has_module_permission(request)

    def session_id_short(self, obj):
        return obj.session_id.hex[:8]
    session_id_short.short_description = "رقم الجلسة"

    def status_badge(self, obj):
        colors = {
            'pending': '#6c757d', 'validating': '#17a2b8', 'preview': '#ffc107',
            'importing': '#007bff', 'completed': '#28a745', 'failed': '#dc3545',
            'rolled_back': '#fd7e14',
        }
        return format_html(
            '<span style="background:{}; color:white; padding:3px 8px; border-radius:4px; font-size:11px; font-weight:bold;">{}</span>',
            colors.get(obj.status, 'gray'), obj.get_status_display()
        )
    status_badge.short_description = "الحالة"


# =====================================================================
# 🛒 17. طلبات النشر في السوق المركزي (B2B Listing Approval)
# =====================================================================
@admin.register(B2BListingRequest)
class B2BListingRequestAdmin(SecureImportExportAdmin):
    list_display = (
        'product', 'status_badge', 'requested_price', 'approved_price',
        'requested_by', 'reviewed_by', 'created_at',
    )
    list_filter = ('status', 'created_at')
    search_fields = ('product__name', 'product__part_number')
    readonly_fields = (
        'product', 'requested_price', 'requested_by',
        'created_at', 'reviewed_at', 'is_synced',
    )
    actions = ['approve_listings', 'reject_listings']

    def has_module_permission(self, request):
        if connection.schema_name == 'public':
            return False
        return super().has_module_permission(request)

    def status_badge(self, obj):
        colors = {'pending': '#f59e0b', 'approved': '#10b981', 'rejected': '#ef4444'}
        return format_html(
            '<span style="background:{}; color:white; padding:3px 10px; '
            'border-radius:4px; font-size:11px; font-weight:bold;">{}</span>',
            colors.get(obj.status, '#6b7280'), obj.get_status_display(),
        )
    status_badge.short_description = "حالة الطلب"

    @admin.action(description='تمت الموافقة — نشر في السوق المركزي')
    def approve_listings(self, request, queryset):
        from inventory.services.inventory_service import InventoryService
        approved = 0
        for listing in queryset.filter(status='pending'):
            price = listing.approved_price or listing.requested_price
            InventoryService.approve_b2b_listing(listing, price, request.user)
            approved += 1
        self.message_user(
            request,
            f"تمت الموافقة على {approved} طلب ونشرها في السوق المركزي بنجاح.",
            messages.SUCCESS,
        )

    @admin.action(description='رفض الطلبات المحددة')
    def reject_listings(self, request, queryset):
        updated = queryset.filter(status='pending').update(
            status='rejected',
            reviewed_by=request.user,
            reviewed_at=timezone.now(),
        )
        self.message_user(request, f"تم رفض {updated} طلب.", messages.WARNING)

# ─────────────────────────────────────────────────────────────────────
# 📩 RFQ Engine — finance & inventory audit trail
# ─────────────────────────────────────────────────────────────────────
from inventory.models import RFQ, RFQQuote


class RFQQuoteInline(admin.TabularInline):
    """Show all per-vendor quotes directly on the RFQ change-form."""
    model = RFQQuote
    extra = 0
    fields = (
        'vendor', 'sent_at', 'quoted_price', 'quoted_eta_days',
        'quoted_at', 'notes',
    )
    readonly_fields = ('sent_at', 'quoted_at')
    autocomplete_fields = ('vendor',)
    can_delete = False
    show_change_link = True


@admin.register(RFQ)
class RFQAdmin(SafeAdminLogMixin, admin.ModelAdmin):
    """Read-mostly audit surface — the actual lifecycle is driven by the
    RFQ engine service; admin only exposes a manual cancel action."""
    list_display = (
        'id', 'part_number_requested', 'part_name_requested', 'quantity',
        'status', 'job_card', 'branch', 'created_at',
        'vendor_count', 'has_winner',
    )
    list_filter = ('status', 'branch', 'created_at')
    search_fields = (
        'part_number_requested', 'part_name_requested',
        'product__part_number', 'product__name',
        'job_card__id',
    )
    autocomplete_fields = ('product', 'job_card', 'branch', 'requested_by')
    readonly_fields = (
        'created_at', 'accepted_quote', 'purchase_invoice',
    )
    inlines = [RFQQuoteInline]
    list_select_related = ('branch', 'job_card', 'product')
    ordering = ('-created_at',)
    actions = ['action_cancel']

    fieldsets = (
        (_("الطلب"), {
            'fields': (
                'part_number_requested', 'part_name_requested', 'quantity',
                'product', 'branch', 'job_card', 'requested_by',
                'notes',
            ),
        }),
        (_("الحالة والنتيجة"), {
            'fields': ('status', 'accepted_quote', 'purchase_invoice',
                       'created_at'),
        }),
    )

    @admin.display(description=_("عدد الموردين"))
    def vendor_count(self, obj):
        return obj.quotes.count()

    @admin.display(boolean=True, description=_("تم اختيار مورد؟"))
    def has_winner(self, obj):
        return obj.accepted_quote_id is not None

    @admin.action(description=_("إلغاء طلبات التسعير المختارة"))
    def action_cancel(self, request, queryset):
        updated = queryset.exclude(
            status__in=[RFQ.STATUS_ORDERED, RFQ.STATUS_CANCELLED],
        ).update(status=RFQ.STATUS_CANCELLED)
        self.message_user(
            request,
            f"تم إلغاء {updated} طلب تسعير.",
            messages.WARNING,
        )


@admin.register(RFQQuote)
class RFQQuoteAdmin(SafeAdminLogMixin, admin.ModelAdmin):
    """Per-vendor quote rows — searchable for finance reconciliation."""
    list_display = (
        'id', 'rfq', 'vendor', 'quoted_price', 'quoted_eta_days',
        'sent_at', 'quoted_at', 'has_response',
    )
    list_filter = ('quoted_at', 'vendor')
    search_fields = (
        'rfq__part_number_requested', 'vendor__name',
        'rfq__product__part_number',
    )
    autocomplete_fields = ('rfq', 'vendor')
    readonly_fields = ('sent_at',)
    list_select_related = ('rfq', 'vendor')
    ordering = ('-sent_at',)

    @admin.display(boolean=True, description=_("ردّ المورد؟"))
    def has_response(self, obj):
        return obj.quoted_price is not None


# ─────────────────────────────────────────────────────────────────────
# 🔮 Predictive Maintenance — rules & nudges
# ─────────────────────────────────────────────────────────────────────
from inventory.models import ServiceReminderRule, ServiceNudge


@admin.register(ServiceReminderRule)
class ServiceReminderRuleAdmin(SafeAdminLogMixin, admin.ModelAdmin):
    """Workshop-tunable maintenance intervals — admin can prune/add freely."""
    list_display = (
        'name', 'category', 'interval_km', 'interval_months',
        'severity', 'is_active', 'brands_label',
    )
    list_filter = ('category', 'severity', 'is_active')
    search_fields = ('name', 'category')
    list_editable = ('is_active', 'severity')

    @admin.display(description=_("الماركات"))
    def brands_label(self, obj):
        return ', '.join(obj.applies_to_brands) if obj.applies_to_brands else 'الكل'


@admin.register(ServiceNudge)
class ServiceNudgeAdmin(SafeAdminLogMixin, admin.ModelAdmin):
    """Audit trail — most operations happen via the CRM dashboard but
    the admin gives finance a queryable backstop."""
    list_display = (
        'id', 'vehicle', 'rule', 'urgency', 'status',
        'due_at', 'last_done_at', 'sent_at', 'sent_by',
    )
    list_filter = ('urgency', 'status', 'rule__category')
    search_fields = (
        'vehicle__chassis_number', 'vehicle__car_plate',
        'vehicle__customer__name', 'vehicle__customer__phone',
        'rule__name',
    )
    # `Vehicle` is registered as a Customer inline (not a top-level admin),
    # so we use raw_id_fields for it; `rule`/`sent_by` have search_fields.
    raw_id_fields = ('vehicle',)
    autocomplete_fields = ('rule', 'sent_by')
    readonly_fields = ('created_at', 'refreshed_at')
    list_select_related = ('vehicle', 'rule', 'sent_by')
    ordering = ('urgency', 'due_at')
