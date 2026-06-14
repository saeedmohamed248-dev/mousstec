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
# B2B listing approvals, RFQ flow, service reminder rules & nudges.

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
