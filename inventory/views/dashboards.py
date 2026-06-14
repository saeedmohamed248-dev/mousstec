"""
🚀 Mouss Tec Enterprise — Views & MAS Orchestrator Layer
=========================================================
المعمارية: كل وكيل (Agent) عبارة عن دالة نقية (Pure Function) تقبل بيانات وترجع بيانات.
الـ Views هي فقط HTTP adapters تستدعي الوكلاء — لا منطق داخل الـ view نفسه.
الـ Orchestrator يُدار بـ async-safe thread pool مع DB connection management صحيح.
"""

from django.shortcuts import render, get_object_or_404, redirect
from django.http import JsonResponse, HttpResponseForbidden, HttpResponse
from django.contrib.auth.decorators import login_required
from django.views.decorators.csrf import csrf_exempt
from django.db.models import Sum, F, Q
from django.utils import timezone
from django.db import connection, transaction, close_old_connections
from django.core.cache import cache
from django.conf import settings
from django_tenants.utils import schema_context
from decimal import Decimal, InvalidOperation

import json
import urllib.parse
import base64
import uuid
import re
import logging
import concurrent.futures

from ..ai_services import predict_parts_from_dtc, scan_invoice_image_ai, call_gemini_layer
from clients.models import GlobalB2BMarketplace, Client, BlindBiddingRequest
from clients.services.entitlements import require_feature

try:
    import qrcode
    from io import BytesIO
except ImportError:
    qrcode = None

from ..models import (
    Product, Inventory, SaleInvoice, SaleInvoiceItem, Branch,
    Customer, Vehicle, ScrapDismantlingJob, ScrapDismantlingYield,
    FinancialTransaction, EmployeeShift, MaintenanceContract, Treasury,
    ChartOfAccount, AccountingEntry, InventoryMovement, StockAlert,
    ImportSession, AuditLog, PurchaseInvoice, Vendor,
)


# Shared utilities live in their own submodule and are re-exported here
# so existing view definitions (defined below) and external imports still see them.
from .utils import *  # noqa: F401, F403
from .utils import _json_response_safe, _get_branch_for_user, _require_tenant  # noqa: F401


# Branch dashboards, POS, mechanic kiosk, b2b marketplace browse.

# =====================================================================
# 📊 1. لوحات التحكم ونقطة البيع وكشك الفنيين
# =====================================================================

@login_required(login_url='/login/')
@tenant_required
def branch_dashboard(request):
    is_admin = request.user.is_superuser or (
        hasattr(request.user, 'employee_profile')
        and request.user.employee_profile.role in ('admin', 'manager')
    )
    branch = _get_branch_for_user(request.user)

    from inventory.services.reporting_service import ReportingService
    raw = ReportingService.get_today_dashboard_stats(request.user, branch)
    today = raw['today']
    low_stock = raw['low_stock_qs']

    # 🐛 [Issue #3 FIX]: نفس الـ source للخزينة المعروضة في /system/dashboard/
    # و /secure-portal/ — يستخدم ReportingService.get_treasury_summary بحيث
    # الفرع المرئي والمجموع لا يختلفان بين الواجهتين.
    treasury = ReportingService.get_treasury_summary(request.user, branch)

    stats = {
        'total_sales_today': raw['total_sales_today'],
        'net_profit_today': (
            raw['net_profit_today'] if is_admin else "🔒 صلاحية المدير فقط"
        ),
        'total_expenses_today': (
            raw['total_expenses_today'] if is_admin else "🔒 صلاحية المدير فقط"
        ),
        'total_treasury': (
            treasury['total_treasury_balance'] if is_admin else "🔒 صلاحية المدير فقط"
        ),
        'treasury_count': treasury['treasury_count'],
        'invoices_count': raw['invoices_count'],
        'low_stock_count': raw['low_stock_count'],
    }

    # Trial / subscription countdown
    tenant = getattr(request, 'tenant', None)
    trial_days_left = None
    sub_days_left = None
    if tenant:
        if tenant.status == 'trial' and getattr(tenant, 'trial_ends_at', None):
            trial_days_left = max(0, (tenant.trial_ends_at - today).days)
        elif tenant.status == 'active' and getattr(tenant, 'subscription_end_date', None):
            sub_days_left = max(0, (tenant.subscription_end_date - today).days)

    # 🛡️ Safely resolve role for the template — reverse OneToOne lookups raise
    # RelatedObjectDoesNotExist which Django templates do NOT silence, so we must
    # resolve it here in Python where hasattr() works correctly.
    current_role = ''
    if hasattr(request.user, 'employee_profile'):
        current_role = request.user.employee_profile.role or ''

    return render(request, 'inventory/dashboard.html', {
        'stats': stats,
        'treasuries_data': treasury['treasuries_data'] if is_admin else [],
        'low_stock_items': low_stock[:10],
        'tenant': tenant,
        'trial_days_left': trial_days_left,
        'sub_days_left': sub_days_left,
        'is_admin': is_admin,
        'current_role': current_role,
        'is_super_user': request.user.is_superuser,
    })


def solutions_tour(request):
    return render(request, 'inventory/solutions.html')


@login_required(login_url='/login/')
@tenant_required
@require_feature('b2b_marketplace')
def b2b_marketplace(request):
    """واجهة سوق B2B التفاعلية مع بحث حي في السوق المركزي.

    🔒 Gated by ``b2b_marketplace`` entitlement — Gold + Empire only.
    Silver tenants get a 403 with an upgrade link (the marketing page
    already promised B2B as a paid feature).
    """
    return render(request, 'inventory/b2b_marketplace.html')


@login_required(login_url='/login/')
@tenant_required
def pos_interface(request):
    return render(request, 'inventory/pos_fast.html')


@login_required(login_url='/login/')
@tenant_required
def mechanic_kiosk_interface(request):
    return render(request, 'inventory/mechanic_bay.html')
